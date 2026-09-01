# -*- coding: utf-8 -*-
from pptx import Presentation

path = r"网易互娱通用PPT模板（含保密）_ExcelAgent答辩优化版.pptx"
p = Presentation(path)

# Theme fonts
for master in p.slide_masters:
    theme = master.element.getroottree() if hasattr(master.element, 'getroottree') else None

import zipfile
from lxml import etree

with zipfile.ZipFile(path) as z:
    names = [n for n in z.namelist() if 'theme' in n.lower() and n.endswith('.xml')]
    print("theme files:", names)
    for n in names[:1]:
        data = z.read(n)
        root = etree.fromstring(data)
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        major = root.find('.//a:fontScheme/a:majorFont/a:latin', ns)
        minor = root.find('.//a:fontScheme/a:minorFont/a:latin', ns)
        major_ea = root.find('.//a:fontScheme/a:majorFont/a:ea', ns)
        minor_ea = root.find('.//a:fontScheme/a:minorFont/a:ea', ns)
        print("major latin:", major.get('typeface') if major is not None else None)
        print("minor latin:", minor.get('typeface') if minor is not None else None)
        print("major ea:", major_ea.get('typeface') if major_ea is not None else None)
        print("minor ea:", minor_ea.get('typeface') if minor_ea is not None else None)

    # dump slideLayout1 and slideMaster1 title placeholder defRPr
    layout_names = sorted([n for n in z.namelist() if 'slideLayouts/slideLayout' in n and n.endswith('.xml')])
    for ln in layout_names[:5]:
        data = z.read(ln)
        root = etree.fromstring(data)
        ns2 = {
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
        }
        print(f"\n--- {ln} ---")
        for sp in root.findall('.//p:sp', ns2):
            ph = sp.find('.//p:nvSpPr/p:nvPr/p:ph', ns2)
            phtype = ph.get('type') if ph is not None else None
            rpr_list = sp.findall('.//a:defRPr', ns2)
            for rpr in rpr_list[:2]:
                print("  ph=", phtype, "sz=", rpr.get('sz'), "b=", rpr.get('b'), "latin=", (rpr.find('a:latin', ns2).get('typeface') if rpr.find('a:latin', ns2) is not None else None))
