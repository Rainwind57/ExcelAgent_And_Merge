"""独立验证：_try_rule_parse_multi 正则模式 (v2: greedy + required separator)"""
import re

add_re = re.compile(
    r'(?:新增|添加|增加|加一个|加一条)\s*'
    r'(?:一个|一条|个)?\s*'
    r'(.+?)(?:名称|的|类型|id|编号)\s*[是为：:]?\s*(.+)',
    re.IGNORECASE
)
del_re = re.compile(
    r'(?:删除|移除|去掉|删掉|清除)\s*'
    r'(?:一个|一条|个)?\s*'
    r'(.+?)(?:名称|id|编号)\s*[是为：:]?\s*(.+)',
    re.IGNORECASE
)
set_re = re.compile(
    r'(?:把|将)\s*(.+?)(?:名称|id|编号)\s*[是为：:]?\s*(.+?)的'
    r'([^\s，,]+?)\s*(?:改为|设为|改成|修改为|换成|调整|变为)\s*(.+)',
    re.IGNORECASE
)

def check(name, m, expected_groups):
    if m and expected_groups:
        got = m.groups()
        ok = all(got[i] == expected_groups[i] for i in range(len(expected_groups)))
        mark = 'OK' if ok else 'FAIL'
        print(f"  {mark}: {name}")
        if not ok:
            print(f"       got={got} expected={expected_groups}")
    elif m is None and expected_groups is None:
        print(f"  OK: {name} → None (correct)")
    else:
        print(f"  FAIL: {name} → {m.groups() if m else None} expected {expected_groups}")

print("=== _try_rule_parse_multi 正则验证 v2 ===")
check("add 灵兽名称朱雀", add_re.match("新增灵兽名称朱雀"), ("灵兽", "朱雀"))
check("add 道具类型TEST1", add_re.match("增加道具类型TEST1"), ("道具", "TEST1"))
check("add 建筑的id为99999", add_re.match("增加建筑的id为99999"), ("建筑", "99999"))
check("delete 神通id 3333", del_re.match("删除神通id为3333的信息"), ("神通", "3333的信息"))
check("set 法宝等级→5", set_re.match("把法宝id为1001的等级改为5"), ("法宝", "1001", "等级", "5"))
check("set 建筑名称→5", set_re.match("把建筑编号为99999的攻击力设为5"), ("建筑", "99999", "攻击力", "5"))
check("跨表无分隔词→None", add_re.match("新增NPC铁匠老张放到entity_prefab"), None)
print("=== 完成 ===")
