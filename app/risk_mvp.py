# app/risk_mvp.py
import re, math
from collections import deque
from enum import Enum

class Axis(str, Enum):
    SI="SI"; IP="IP"; MA="MA"; PB="PB"; HB="HB"; AS="AS"; PF="PF"

WEIGHTS = {Axis.SI:10, Axis.IP:12, Axis.MA:10, Axis.PB:12, Axis.HB:8, Axis.AS:6, Axis.PF:-8}
HALF_LIFE_TURNS = 20
LOW_TH = 15; HIGH_TH = 45
OVERRIDE_RECENT_TURNS = 2

P_NEG   = re.compile(r"(않|안|싶지\s*않|싶진\s*않)")
P_META  = re.compile(r"(뉴스|기사|드라마|가사|영화|예시|논문|수업|연구)")
P_THIRD = re.compile(r"(친구|사람들|누가|그[가녀])")
P_IDIOM = re.compile(r"(죽을맛|웃겨\s*죽|맛\s*죽이)")

P_SI_DIRECT   = re.compile(r"(자살|목숨\s*끊|삶\s*끝내|죽으면\s*편하)")
P_SI_INDIRECT = re.compile(r"(죽고\s*싶|살기\s*싫|사라지고\s*싶|없어지고\s*싶)")
P_IP_PLAN     = re.compile(r"(끝내고\s*싶|포기할래|유서)")
P_MA_MEANS    = re.compile(r"(옥상|난간|약\s*먹|과다\s*복용|수면제|번개탄|로프|칼)")
P_HOPELESS    = re.compile(r"(희망이\s*없|의미\s*없|쓸모\s*없)")
P_STRESS      = re.compile(r"(힘들|지쳤|우울|무기력|하기\s*싫)")
P_PAST        = re.compile(r"(예전에|한때|옛날에|과거에)")

def _flags(t:str):
    return dict(
        neg=bool(P_NEG.search(t)), meta=bool(P_META.search(t)),
        third=bool(P_THIRD.search(t)), idiom=bool(P_IDIOM.search(t)), past=bool(P_PAST.search(t))
    )

def rule_levels(text:str):
    t = (text or "").strip().lower()
    flags = _flags(t)
    if flags["idiom"] or flags["meta"] or flags["third"]:
        return {a:0 for a in Axis}, flags, []

    ev = []; axes = {a:0 for a in Axis}
    def hit(axis, lvl, pat, rid):
        m = pat.search(t)
        if not m: return
        s,e = m.span()
        lvl2 = 0 if flags["neg"] else lvl
        if flags["past"] and axis in (Axis.SI, Axis.IP):
            lvl2 = max(0, lvl2-1)
        axes[axis] = max(axes[axis], lvl2)
        ev.append(dict(axis=axis, lvl=lvl2, excerpt=t[max(0,s-6):min(len(t),e+6)], rule=rid))

    hit(Axis.SI,3,P_SI_DIRECT,"si_direct_03")
    hit(Axis.SI,2,P_SI_INDIRECT,"si_indirect_02")
    hit(Axis.IP,2,P_IP_PLAN,"ip_plan_02")
    hit(Axis.MA,3,P_MA_MEANS,"ma_means_03")
    hit(Axis.HB,2,P_HOPELESS,"hb_02")
    hit(Axis.AS,1,P_STRESS,"as_01")
    return axes, flags, ev

class RiskWindow:
    def __init__(self, maxlen=20):
        self.q = deque(maxlen=maxlen)
    def add(self, axes_levels): self.q.append(axes_levels)
    def score(self):
        lam = math.log(2)/HALF_LIFE_TURNS
        total=0.0; n=len(self.q)
        for i, levels in enumerate(self.q):
            decay = math.exp(-lam*(n-i-1))
            for a,lvl in levels.items():
                total += WEIGHTS[a]*lvl*decay
        score = int(max(0,min(100, round(2*total))))
        return score
    def recent_max(self, axis:Axis, k=OVERRIDE_RECENT_TURNS):
        arr = list(self.q)[-k:];  return max((d.get(axis,0) for d in arr), default=0)

def evaluate_turn(window:RiskWindow, text:str):
    axes, flags, ev = rule_levels(text)
    window.add(axes)
    score = window.score()
    override = (window.recent_max(Axis.IP)>=3) or (window.recent_max(Axis.SI)>=3 and window.recent_max(Axis.MA)>=3)
    band = "imminent" if override else ("high" if score>=HIGH_TH else "low" if score<=LOW_TH else "moderate")
    return band, score, {"axes":axes, "flags":flags, "evidence":ev, "override":override, "score":score}
