#!/usr/bin/env python3
"""Chronological, leakage-aware calibration/backtest for SPL launch JSONL data."""
import argparse, json, math, pathlib, sys
from spl_rug_radar import score_launch

def pav(rows):
    """Pool-adjacent-violators isotonic fit. Returns [upper_score, calibrated_rate]."""
    blocks=[]
    for p,y in sorted(rows):
        blocks.append([p,p,float(y),1])
        while len(blocks)>1 and blocks[-2][2]/blocks[-2][3] > blocks[-1][2]/blocks[-1][3]:
            b=blocks.pop(); a=blocks.pop()
            blocks.append([a[0],b[1],a[2]+b[2],a[3]+b[3]])
    return [[round(b[1],8),round(b[2]/b[3],8)] for b in blocks]

def apply(p, knots):
    for edge,value in knots:
        if p<=edge:return value
    return knots[-1][1]

def metrics(rows):
    tp=sum(p>=.5 and y for p,y in rows); fp=sum(p>=.5 and not y for p,y in rows)
    tn=sum(p<.5 and not y for p,y in rows); fn=sum(p<.5 and y for p,y in rows)
    n=len(rows); ece=0.0
    for i in range(10):
        g=[(p,y) for p,y in rows if i/10<=p< (i+1)/10 or (i==9 and p==1)]
        if g: ece+=len(g)/n*abs(sum(p for p,y in g)/len(g)-sum(y for p,y in g)/len(g))
    return {"n":n,"tp":tp,"fp":fp,"tn":tn,"fn":fn,
      "precision":round(tp/max(1,tp+fp),4),"recall":round(tp/max(1,tp+fn),4),
      "f1":round(2*tp/max(1,2*tp+fp+fn),4),
      "brier":round(sum((p-y)**2 for p,y in rows)/n,5),"ece":round(ece,5)}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("jsonl"); ap.add_argument("--calibration",default="calibration.json")
    ap.add_argument("--min-samples",type=int,default=50); args=ap.parse_args()
    data=[]
    for line_no,line in enumerate(pathlib.Path(args.jsonl).read_text().splitlines(),1):
        if not line.strip():continue
        row=json.loads(line); label=row.pop("label")
        if label not in (0,1,False,True):raise ValueError(f"line {line_no}: label must be 0 or 1")
        pred=score_launch(row)
        if pred["in_window"] and pred["rug_probability"] is not None:
            data.append((int(row["launch_slot"]),float(pred["rug_probability"]),int(label)))
    data.sort()
    if len(data)<args.min_samples:raise SystemExit(f"need at least {args.min_samples} labeled in-window launches; got {len(data)}")
    cut=max(1,min(len(data)-1,int(len(data)*.7)))
    train,test=data[:cut],data[cut:]
    if len({x[2] for x in train})<2 or len({x[2] for x in test})<2:
        raise SystemExit("chronological train and holdout partitions must each contain both labels")
    knots=pav([(p,y) for _,p,y in train])
    raw=[(p,y) for _,p,y in test]; calibrated=[(apply(p,knots),y) for _,p,y in test]
    artifact={"method":"isotonic-pav","fit":"chronological first 70%; holdout last 30%",
      "train_n":len(train),"holdout_n":len(test),"knots":knots,
      "warning":"Calibration is valid only for the documented data source, label definition and time period."}
    pathlib.Path(args.calibration).write_text(json.dumps(artifact,indent=2)+"\n")
    print(json.dumps({"dataset_n":len(data),"train":metrics([(p,y) for _,p,y in train]),
      "holdout_raw":metrics(raw),"holdout_calibrated":metrics(calibrated),
      "calibration":args.calibration},indent=2))
if __name__=="__main__":main()
