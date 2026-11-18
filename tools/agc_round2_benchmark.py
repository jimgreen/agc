"""Full-module paired comparison against c8f32ce; no production DB or SCADA."""
import copy, json, logging, random, statistics, subprocess, sys, time, types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import agc_ctrl
from tools import agc_stress_test as stress


def load_baseline():
    source=subprocess.check_output(['git','show','c8f32ce:agc_ctrl.py'],cwd=ROOT,text=True,encoding='utf-8')
    module=types.ModuleType('agc_before_c8f32ce')
    module.__file__=str(ROOT/'agc_ctrl.py')
    sys.modules[module.__name__]=module
    exec(compile(source,'<c8f32ce>','exec'),module.__dict__)
    return module


def outcome(model):
    return {'devices':[(kind,str(d.id),d.p_ctrl,d.p_cur) for kind,d in stress._all_devices(model)],
            'acceptance':model.agc_renewable_acceptance,'mismatch':model.agc_balance_mismatch,
            'commands':model.config.commands}


def main():
    old=load_baseline(); rng=random.Random(20261118)
    base=stress.efile_factory_from_file(ROOT/'model_real.e')
    models=[stress._prepare_case(base,rng,stress._scenario_for(i,rng))[0] for i in range(1000)]
    logger=logging.Logger('paired-control',level=logging.CRITICAL)
    for original in models:
        left,right=copy.deepcopy(original),copy.deepcopy(original)
        old.do_wind_solar_priority_ctrl(logger,left); agc_ctrl.do_wind_solar_priority_ctrl(logger,right)
        assert outcome(left)==outcome(right)
    rounds=[]
    for round_no in range(11):
        sample={}
        for name,module in ([('before',old),('after',agc_ctrl)][::1 if round_no%2==0 else -1]):
            inputs=[copy.deepcopy(model) for model in models]
            begin=time.perf_counter()
            for model in inputs: module.do_wind_solar_priority_ctrl(logger,model)
            sample[name]=(time.perf_counter()-begin)*1e6/len(inputs)
        sample['reduction_percent']=100*(1-sample['after']/sample['before'])
        rounds.append(sample)
    result={'baseline':'c8f32ce','seed':20261118,'equivalent_cases':len(models),'unit':'microseconds per control call',
        'rounds':rounds,'median_before':statistics.median(r['before'] for r in rounds),
        'median_after':statistics.median(r['after'] for r in rounds),
        'median_paired_reduction_percent':statistics.median(r['reduction_percent'] for r in rounds),
        'improved_rounds':sum(r['after']<r['before'] for r in rounds)}
    target=ROOT/'output/agc_round2_benchmark_20261118.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rounds'},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
