#!/usr/bin/env python3
"""Read-only Solana RPC/WebSocket launch ingester; emits scorer JSON for mint+pool events.
Uses only Python 3.12 stdlib. Configure venue program IDs with --dex-program.
"""
import argparse,base64,hashlib,json,os,random,socket,ssl,struct,sys,time
from urllib.parse import urlparse
from urllib.request import Request,urlopen
from spl_rug_radar import score_launch

TOKEN="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RAYDIUM_V4="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
BURN={"1nc1nerator11111111111111111111111111111111","dead"}

def rpc(url,method,params):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=Request(url,data=body,headers={"Content-Type":"application/json"})
    with urlopen(req,timeout=20) as r: out=json.load(r)
    if "error" in out: raise RuntimeError(out["error"])
    return out.get("result")

def exact(sock,n):
    b=b""
    while len(b)<n:
        x=sock.recv(n-len(b))
        if not x: raise ConnectionError("WebSocket closed")
        b+=x
    return b

def ws_connect(url):
    u=urlparse(url); port=u.port or (443 if u.scheme=="wss" else 80)
    s=socket.create_connection((u.hostname,port),timeout=30)
    if u.scheme=="wss": s=ssl.create_default_context().wrap_socket(s,server_hostname=u.hostname)
    key=base64.b64encode(os.urandom(16)).decode()
    path=u.path or "/"
    if u.query:path+="?"+u.query
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head=b""
    while b"\r\n\r\n" not in head:
        part=s.recv(1)
        if not part: raise ConnectionError("WebSocket closed during handshake")
        head+=part
    if b" 101 " not in head: raise ConnectionError("WebSocket upgrade rejected: "+head.decode(errors="replace"))
    accept=base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
    headers={line.split(b":",1)[0].strip().lower():line.split(b":",1)[1].strip() for line in head.split(b"\r\n")[1:] if b":" in line}
    if headers.get(b"sec-websocket-accept")!=accept: raise ConnectionError("bad WebSocket accept")
    return s

def ws_send(s,obj):
    b=json.dumps(obj,separators=(",",":")).encode(); n=len(b)
    h=bytes([0x81,0x80|(n if n<126 else 126 if n<65536 else 127)])
    if n>=126:h+=struct.pack("!H" if n<65536 else "!Q",n)
    mask=os.urandom(4); s.sendall(h+mask+bytes(x^mask[i%4] for i,x in enumerate(b)))

def ws_recv(s):
    fragments=[]
    message_opcode=None
    while True:
        a,b=exact(s,2); fin=bool(a&128); op=a&15; n=b&127
        if n==126:n=struct.unpack("!H",exact(s,2))[0]
        elif n==127:n=struct.unpack("!Q",exact(s,8))[0]
        mask=exact(s,4) if b&128 else None; data=exact(s,n)
        if mask:data=bytes(x^mask[i%4] for i,x in enumerate(data))
        if op==8:raise ConnectionError("WebSocket close frame")
        if op==9:
            mask=os.urandom(4); payload=bytes(x^mask[i%4] for i,x in enumerate(data))
            s.sendall(bytes([0x8a,0x80|len(data)])+mask+payload);continue
        if op==10:continue
        if op==1:
            if message_opcode is not None:raise ConnectionError("nested WebSocket data frame")
            message_opcode=op
            fragments=[data]
            if fin:return json.loads(data)
        elif op==0 and message_opcode==1:
            fragments.append(data)
            if fin:return json.loads(b"".join(fragments))
        elif op in (2,):
            if not fin:message_opcode=op
            else:message_opcode=None
            fragments=[]
        elif op==0:
            continue

def instructions(tx):
    tx=tx or {}; msg=tx.get("transaction",{}).get("message") or tx.get("message",{})
    out=list(msg.get("instructions",[]))
    for group in tx.get("meta",{}).get("innerInstructions",[]):out.extend(group.get("instructions",[]))
    return out

def parsed_info(ix):
    p=ix.get("parsed")
    return (p.get("type","").lower(),p.get("info",{})) if isinstance(p,dict) else ("",{})

def launch_mints(tx):
    found=[]
    for ix in instructions(tx):
        typ,info=parsed_info(ix)
        if typ in ("initializemint","initializemint2") and info.get("mint"):found.append(info["mint"])
    return found

def tx_mints(tx):
    meta=(tx or {}).get("meta",{})
    return {x.get("mint") for x in meta.get("postTokenBalances",[]) if x.get("mint")}

class LaunchCorrelator:
    """Bounded, slot-aware mint/pool joiner tolerant of notification reordering."""
    def __init__(self,window=15,limit=10000):
        self.window=window; self.limit=limit
        self.mints={}; self.pools={}

    def _prune(self,slot):
        for cache in (self.mints,self.pools):
            for mint,item in list(cache.items()):
                event_slot=item["slot"] if "slot" in item else item["pool_slot"]
                if slot-event_slot>self.window:cache.pop(mint,None)
            while len(cache)>self.limit:cache.pop(next(iter(cache)))

    def add_mint(self,mint,launch,current_slot):
        self._prune(current_slot)
        pool=self.pools.pop(mint,None)
        if pool:
            age=pool["pool_slot"]-launch["slot"]
            if 0<=age<=self.window:
                return launch,pool["pool"],pool["tx"],pool["pool_slot"]
        self.mints[mint]=launch
        while len(self.mints)>self.limit:self.mints.pop(next(iter(self.mints)))
        return None

    def add_pool(self,mint,pool,tx,pool_slot,current_slot):
        self._prune(current_slot)
        launch=self.mints.pop(mint,None)
        if launch:
            age=pool_slot-launch["slot"]
            if 0<=age<=self.window:
                return launch,pool,tx,pool_slot
        self.pools[mint]={"pool":pool,"tx":tx,"pool_slot":pool_slot}
        while len(self.pools)>self.limit:self.pools.pop(next(iter(self.pools)))
        return None


def b58decode(value):
    alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n=0
    for ch in value:n=n*58+alphabet.index(ch)
    raw=n.to_bytes((n.bit_length()+7)//8,"big") if n else b""
    return b"\0"*(len(value)-len(value.lstrip("1")))+raw

def pool_info(tx,dex_ids):
    for ix in instructions(tx):
        pid=ix.get("programId")
        typ,info=parsed_info(ix)
        if pid in dex_ids and (any(w in typ for w in ("initialize","create","open")) or (pid==RAYDIUM_V4 and ix.get("data"))):
            mints={v for k,v in info.items() if "mint" in k.lower() and isinstance(v,str) and len(v)>30}
            vaults=[v for k,v in info.items() if ("vault" in k.lower() or "tokenaccount" in k.lower()) and isinstance(v,str)]
            lp=next((v for k,v in info.items() if "lp" in k.lower() and "mint" in k.lower() and isinstance(v,str)),None)
            if not mints and pid==RAYDIUM_V4 and ix.get("data"):
                raw=b58decode(ix["data"]); a=ix.get("accounts",[])
                if raw and raw[0]==1 and len(a) in (19,21):
                    lp_i,coin_i,pc_i,vcoin_i,vpc_i=(6,7,8,9,10) if len(a)==19 else (7,8,9,10,11)
                    lp=a[lp_i];mints={a[coin_i],a[pc_i]};vaults=[a[vcoin_i],a[vpc_i]]
            if not mints:mints=tx_mints(tx)
            if mints:return {"mints":mints,"vaults":vaults,"lp_mint":lp}
    return None

def account_info(url,key):
    r=rpc(url,"getAccountInfo",[key,{"encoding":"jsonParsed","commitment":"confirmed"}])
    return (r or {}).get("value")

def holders(url,mint,vaults):
    totals={}
    for program in (TOKEN,TOKEN22):
        try:
            rows=rpc(url,"getProgramAccounts",[program,{"encoding":"jsonParsed","commitment":"confirmed","filters":[{"memcmp":{"offset":0,"bytes":mint}}]}]) or []
        except Exception:rows=[]
        for row in rows:
            data=row.get("account",{}).get("data",{}).get("parsed",{}).get("info",{})
            owner=data.get("owner"); amount=data.get("tokenAmount",{}).get("amount","0")
            if owner and row.get("pubkey") not in vaults:
                totals[owner]=totals.get(owner,0)+int(amount)
    if not totals:
        try:largest=rpc(url,"getTokenLargestAccounts",[mint])["value"]
        except Exception:largest=[]
        for item in largest:
            try:
                data=account_info(url,item["address"])["data"]["parsed"]["info"]
                owner=data["owner"]
                if item["address"] not in vaults:totals[owner]=totals.get(owner,0)+int(item["amount"])
            except Exception:pass
    return totals

def lp_proof(url,tx,lp_mint,lock_ids,slot,lock_tx=None):
    if not lp_mint:return 0.0,False,0.0,0,False
    initial=0
    for ix in instructions(tx):
        typ,info=parsed_info(ix)
        if typ in ("mintto","minttochecked") and info.get("mint")==lp_mint:
            try:initial+=int(info.get("amount",0))
            except (ValueError,TypeError):pass
    try:
        supply=int(rpc(url,"getTokenSupply",[lp_mint])["value"]["amount"])
    except Exception:return 0.0,False,0.0,0,False
    # A decrease from the observed initial mint-to amount is already the
    # on-chain burn evidence. Adding balances held by burn addresses would
    # double-count those same tokens, because burned tokens are absent from
    # current supply. If supply exceeds the observed initial amount, later
    # minting or incomplete initial-mint evidence makes the result unknown.
    if initial and initial>=supply:
        pct=min(100,100*(initial-supply)/initial)
        burn_ok=pct>0
    else:pct=0.0;burn_ok=False
    locked=0.0;days=0;lock_ok=False
    # A lock is accepted only from a configured program's parsed lock instruction
    # carrying the LP mint, amount and a future unlock timestamp.
    try:now=rpc(url,"getBlockTime",[slot])
    except Exception:now=None
    if now:
        for ix in instructions(lock_tx or tx):
            typ,info=parsed_info(ix)
            if ix.get("programId") in lock_ids and "lock" in typ and info.get("mint",info.get("lpMint"))==lp_mint:
                expiry=info.get("unlockTime",info.get("unlockTimestamp"))
                amount=info.get("amount")
                vault=next((info.get(k) for k in ("tokenAccount","lpTokenAccount","vault","escrow","destination") if isinstance(info.get(k),str)),None)
                try:
                    amount=int(amount)
                    # A parsed instruction alone is not enough: verify its LP
                    # destination is a live token account holding at least the
                    # amount claimed by the locker instruction.
                    va=account_info(url,vault) if vault else None
                    vi=va["data"]["parsed"]["info"] if va else {}
                    balance=int(vi.get("tokenAmount",{}).get("amount",0))
                    if (int(expiry)>now and initial and 0<amount<=initial
                            and vi.get("mint")==lp_mint and balance>=amount):
                        locked=max(locked,100*amount/initial)
                        days=max(days,int((int(expiry)-now)/86400));lock_ok=True
                except (ValueError,TypeError,KeyError):pass
    return min(100,pct),burn_ok,min(100,locked),days,lock_ok


def slot_metrics(url,mint,start,end):
    """Measure early buys, shared SOL funders and same-wallet round trips."""
    buys=[]; funders0=set(); same_slot={}; buyers=set(); sellers=set()
    for slot in range(start,end+1):
        try:block=rpc(url,"getBlock",[slot,{"encoding":"jsonParsed","transactionDetails":"full","rewards":False,"maxSupportedTransactionVersion":1}])
        except Exception:continue
        entries=(block or {}).get("transactions",[]); funding={}
        for entry in entries:
            for ix in instructions(entry.get("transaction",{})):
                typ,info=parsed_info(ix)
                if typ=="transfer" and info.get("source") and info.get("destination"):
                    funding[info["destination"]]=info["source"]
        for entry in entries:
            tx=entry.get("transaction",{}); meta=entry.get("meta") or {}
            keys=tx.get("message",{}).get("accountKeys",[])
            payer=(keys[0].get("pubkey") if keys and isinstance(keys[0],dict) else keys[0] if keys else None)
            signers={x.get("pubkey") for x in keys if isinstance(x,dict) and x.get("signer")}
            pre={}; post={}
            for x in meta.get("preTokenBalances",[]):
                if x.get("mint")==mint:pre[x.get("accountIndex")]=(int(x.get("uiTokenAmount",{}).get("amount",0)),x.get("owner"))
            for x in meta.get("postTokenBalances",[]):
                if x.get("mint")==mint:post[x.get("accountIndex")]=(int(x.get("uiTokenAmount",{}).get("amount",0)),x.get("owner"))
            delta=0
            for i in set(pre)|set(post):
                before,bo=pre.get(i,(0,None)); after,ao=post.get(i,(0,None))
                d=after-before; owner=ao or bo
                if d>0 and owner in signers:
                    delta+=d;buyers.add(owner)
                elif d<0 and owner in signers:sellers.add(owner)
            if delta>0:
                buys.append(float(delta));same_slot[slot]=same_slot.get(slot,0)+1
                if slot==start:funders0.add(funding.get(payer,payer))
    n=len(buys);mean=sum(buys)/n if n else 0
    cv=(sum((x-mean)**2 for x in buys)/n)**.5/mean if mean else 1.0
    return {"slot0_buys":same_slot.get(start,0),"total_early_buys":n,
      "unique_funders_in_slot0":len(funders0),"buy_amount_cv":cv,
      "jito_bundle_count":0,"wash_roundtrip_ratio":len(buyers&sellers)/max(1,len(buyers))}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rpc",default=os.getenv("SOLANA_RPC_URL","https://api.mainnet-beta.solana.com"))
    ap.add_argument("--ws",default=os.getenv("SOLANA_WS_URL","wss://api.mainnet-beta.solana.com"))
    ap.add_argument("--dex-program",action="append",default=[])
    ap.add_argument("--lock-program",action="append",default=[])
    ap.add_argument("--calibration",help="optional isotonic calibration.json from backtest.py")
    args=ap.parse_args()
    calibration=json.load(open(args.calibration)) if args.calibration else None
    if not args.dex_program:ap.error("at least one --dex-program is required; use exact pool program IDs")
    programs=[TOKEN,TOKEN22]+args.dex_program+args.lock_program
    correlator=LaunchCorrelator()
    sub_program={}; seen={}; pool_by_lp={}
    while True:
        s=None
        def emit(mint,launch,pool,pool_tx,event_tx,slot):
            age=slot-launch["slot"]
            if age<0 or age>15:return
            ai=account_info(args.rpc,mint)
            try:mi=ai["data"]["parsed"]["info"]
            except (TypeError,KeyError):return
            hs=holders(args.rpc,mint,set(pool["vaults"]))
            burn,bv,lock,days,lv=lp_proof(args.rpc,pool_tx,pool["lp_mint"],set(args.lock_program),slot,event_tx)
            activity=slot_metrics(args.rpc,mint,launch["slot"],slot)
            event={"mint":mint,"launch_slot":launch["slot"],"event_slot":slot,
              "mint_authority_active":bool(mi.get("mintAuthority")),"freeze_authority_active":bool(mi.get("freezeAuthority")),
              "holders":hs,"lp_vaults":pool["vaults"],"lp_burn_pct":burn,"lp_burn_verified":bv,
              "lp_lock_pct":lock,"lp_lock_days":days,"lp_lock_verified":lv,**activity}
            result=score_launch(event)
            if calibration and result["rug_probability"] is not None:
                from calibrated_scorer import calibrate
                result["rug_probability"]=round(calibrate(result["rug_probability"],calibration["knots"]),4)
                result["momentum_probability"]=round(1-result["rug_probability"],4)
                result["calibration"]="isotonic; trained on "+str(calibration["train_n"])+" chronological samples"
            result["ingestion"]={"signature":event_tx.get("transaction",{}).get("signatures",[""])[0],
              "pool_programs":args.dex_program,"source":"Solana JSON-RPC WebSocket + parsed transaction"}
            print(json.dumps(result,separators=(",",":")),flush=True)
        try:
            s=ws_connect(args.ws)
            for i,pid in enumerate(programs,1):
                ws_send(s,{"jsonrpc":"2.0","id":i,"method":"logsSubscribe","params":[{"mentions":[pid]},{"commitment":"confirmed"}]})
            while True:
                msg=ws_recv(s)
                if "id" in msg and "result" in msg:
                    sub_program[msg["result"]]=programs[msg["id"]-1];continue
                result=msg.get("params",{}).get("result",{}); value=result.get("value",{})
                if value.get("err"):continue
                slot=result.get("context",{}).get("slot"); sig=value.get("signature")
                pid=sub_program.get(msg.get("params",{}).get("subscription"))
                key=(sig,pid)
                if slot is None or not sig or key in seen:continue
                logs=value.get("logs",[])
                if pid in (TOKEN,TOKEN22) and not any("InitializeMint" in x for x in logs):continue
                if pid in args.dex_program and not any(any(k in x.lower() for k in ("initialize","create","open")) for x in logs):continue
                seen[key]=slot
                if len(seen)>20000:seen.pop(next(iter(seen)))
                try:tx=rpc(args.rpc,"getTransaction",[sig,{"encoding":"jsonParsed","commitment":"confirmed","maxSupportedTransactionVersion":1}])
                except Exception:continue
                if not tx:continue
                if pid in (TOKEN,TOKEN22):
                    for mint in launch_mints(tx):
                        match=correlator.add_mint(mint,{"slot":slot,"mint_tx":tx},slot)
                        if match:
                            launch,pool,pool_tx,pool_slot=match
                            if pool["lp_mint"]:
                                pool_by_lp[pool["lp_mint"]]=(mint,launch,pool,pool_tx)
                                if len(pool_by_lp)>10000:pool_by_lp.pop(next(iter(pool_by_lp)))
                            emit(mint,launch,pool,pool_tx,pool_tx,pool_slot)
                    continue
                pool=pool_info(tx,set(args.dex_program)) if pid in args.dex_program else None
                if pool:
                    for mint in pool["mints"]:
                        match=correlator.add_pool(mint,pool,tx,slot,slot)
                        if not match:continue
                        launch,matched_pool,pool_tx,pool_slot=match
                        if matched_pool["lp_mint"]:
                            pool_by_lp[matched_pool["lp_mint"]]=(mint,launch,matched_pool,pool_tx)
                            if len(pool_by_lp)>10000:pool_by_lp.pop(next(iter(pool_by_lp)))
                        emit(mint,launch,matched_pool,pool_tx,pool_tx,pool_slot)
                    continue
                if pid in args.lock_program:
                    for ix in instructions(tx):
                        typ,info=parsed_info(ix)
                        lp=info.get("lpMint",info.get("mint"))
                        saved=pool_by_lp.get(lp)
                        if saved and "lock" in typ:
                            mint,launch,pool,pool_tx=saved
                            emit(mint,launch,pool,pool_tx,tx,slot)
        except (ConnectionError,OSError,TimeoutError) as e:
            print("stream reconnect:",e,file=sys.stderr);time.sleep(2)
        finally:
            if s:s.close()

if __name__=="__main__":main()
