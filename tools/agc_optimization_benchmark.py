"""Paired benchmark; git baseline functions, identical seeded inputs, no runtime DB."""
import ast
import copy
import json
import logging
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import agc_ctrl
from tools import agc_stress_test as stress


def main():
    baseline='8db7b2d'
    source=subprocess.check_output(['git','show',f'{baseline}:agc_ctrl.py'],cwd=ROOT,text=True,encoding='utf-8')
    tree=ast.parse(source)
    names={'do_wind_solar_priority_ctrl','_invoke_scada_callable'}
    namespace=dict(vars(agc_ctrl))
    for node in tree.body:
        if isinstance(node,ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<baseline>','exec'),namespace)
    logger=logging.Logger('benchmark',level=logging.CRITICAL)
    rng=random.Random(20261118)
    base=stress.efile_factory_from_file(ROOT/'model_real.e')
    models=[stress._prepare_case(base,rng,stress._scenario_for(i,rng))[0] for i in range(300)]
    pairs={name:[] for name in ['controller','scada_adapter']}
    read=lambda rtu,pnt: (True,'point',float(rtu+pnt))
    old_ctrl,new_ctrl=namespace['do_wind_solar_priority_ctrl'],agc_ctrl.do_wind_solar_priority_ctrl
    old_read,new_read=namespace['_invoke_scada_callable'],agc_ctrl._invoke_scada_callable
    for original in models:
        left,right=copy.deepcopy(original),copy.deepcopy(original)
        old_ctrl(logger,left); new_ctrl(logger,right)
        a=[(kind,str(d.id),d.p_ctrl,d.p_cur) for kind,d in stress._all_devices(left)]
        b=[(kind,str(d.id),d.p_ctrl,d.p_cur) for kind,d in stress._all_devices(right)]
        assert a==b, 'decision mismatch'
    for round_no in range(9):
        times={}
        for label,ctrl,invoke in ([('before',old_ctrl,old_read),('after',new_ctrl,new_read)][::1 if round_no%2==0 else -1]):
            inputs=[copy.deepcopy(model) for model in models]
            start=time.perf_counter()
            for model in inputs: ctrl(logger,model)
            times[label]=(time.perf_counter()-start)*1e6/len(inputs)
            start=time.perf_counter()
            for i in range(10000): assert invoke(read,None,None,1,i)==(True,'point',float(i+1))
            times[label+'_adapter']=(time.perf_counter()-start)*1e6/10000
        pairs['controller'].append([times['before'],times['after']])
        pairs['scada_adapter'].append([times['before_adapter'],times['after_adapter']])
    result={'baseline':baseline,'rounds':9,'decision_equivalent_cases':300,'unit':'microseconds per call','samples':pairs,'summary':{}}
    for name,rows in pairs.items():
        before=statistics.median(r[0] for r in rows); after=statistics.median(r[1] for r in rows)
        result['summary'][name]={'before':before,'after':after,'reduction_percent':100*(1-after/before)}
    out=ROOT/'output'/'agc_optimization_benchmark_20261118.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['summary'],ensure_ascii=False,indent=2))
    print(out)

if __name__=='__main__': main()
