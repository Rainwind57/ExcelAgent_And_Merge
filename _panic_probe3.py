import sys, io, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
out = io.StringIO()

from python_calamine import CalamineWorkbook
out.write("calamine version: %s\n" % getattr(CalamineWorkbook, "__module__", ""))

# 直接测：读 item.xlsx 的 ItemBase sheet，iter_rows 是否 panic，BaseException 能否接住
path = "resources/item/item.xlsx"
out.write("path exists: %s\n" % os.path.exists(path))
try:
    cw = CalamineWorkbook.from_path(path)
    out.write("sheets: %r\n" % cw.sheet_names)
    sh = cw.get_sheet_by_name("ItemBase")
    out.write("got sheet ItemBase: %r\n" % (sh,))
    it = sh.iter_rows()
    first = next(it, None)
    second = next(it, None)
    out.write("first: %r\n" % (first,))
except BaseException as e:
    out.write("CAUGHT BaseException: %s: %s\n" % (type(e).__name__, e))

with open("_panic_probe3.txt", "w", encoding="utf-8") as f:
    f.write(out.getvalue())
