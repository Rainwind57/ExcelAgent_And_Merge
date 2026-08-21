"""列出三模式所有冲突/修改/增删的表与 sheet，供实际操作验证 compare。"""
import sys
sys.path.insert(0, 'server')
from routers.merge_branch import branch_compare, BranchCompareRequest
from routers.merge_subdir import subdir_compare, SubdirCompareRequest

def dump(title, r):
    print(f'\n{title}')
    print('-' * 60)
    total_c = total_chg = total_ins = total_del = 0
    for gn in sorted(r.groups):
        g = r.groups[gn]
        for sn in sorted(g.sheets):
            st = g.sheets[sn].stats
            if st['conflicts'] or st['changed'] or st['inserted'] or st['deleted']:
                print(f'  {gn}/{sn}: 冲突={st["conflicts"]} 修改={st["changed"]} 新增行={st["inserted"]} 删除行={st["deleted"]} 漏行={st["missing_rows"]}')
            total_c += st['conflicts']; total_chg += st['changed']
            total_ins += st['inserted']; total_del += st['deleted']
    print(f'  --- 汇总: 冲突={total_c} 修改={total_chg} 新增={total_ins} 删除={total_del}')
    sc = [(s.kind, s.table, s.sheet, s.status) for s in r.structural_changes]
    if sc:
        print(f'  结构增删 ({len(sc)}):')
        for k, t, s, st in sc:
            print(f'    [{k}] {t}/{s} = {st}')

# absorb（只比有冲突的小表，避开 monster 10w 行慢路径）
dump('=== ABSORB: dev1 -> dev2 ===',
     branch_compare(BranchCompareRequest(direction='absorb',
         source_branch='svn/demo_svn/wc/branches/dev1',
         target_branch='svn/demo_svn/wc/branches/dev2',
         group_names=['item/item', 'reward'])))

# merge_back
dump('=== MERGE_BACK: dev1 -> trunk ===',
     branch_compare(BranchCompareRequest(direction='merge_back',
         source_branch='svn/demo_svn/wc/branches/dev1',
         target_branch='svn/demo_svn/wc/trunk',
         group_names=['item/item', 'reward'])))

# subdir（只比 subdev_1 有的小表）
dump('=== 目录合并: trunk/subdev_1 -> trunk ===',
     subdir_compare(SubdirCompareRequest(
         source_dir='svn/demo_svn/wc/trunk/subdev_1',
         target_dir='svn/demo_svn/wc/trunk',
         group_names=['ability', 'item_drop'])))
