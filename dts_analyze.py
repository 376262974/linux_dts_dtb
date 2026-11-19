"""
dts_analyze.py - DTS/DTB 内容分析器

从设备树中提炼出人类可读的硬件信息：
  * 板卡 / SoC / CPU 型号
  * 内存与保留区
  * 外设清单（按类别分组，含 compatible、状态、寄存器地址）
  * GPIO 引脚使用（哪些 pads 被占用、方向、标志）
  * pinctrl 引脚复用（引脚功能）
  * 时钟与频率
  * 中断

只依赖 dtb_engine 的解析结果，纯标准库。
"""

import re

import dtb_engine as E


# ---------------------------------------------------------------------------
# 属性读取辅助
# ---------------------------------------------------------------------------

def _prop(node, name):
    """返回节点中名为 name 的属性 parts（可能多个），找不到返回 []。"""
    out = []
    for pname, parts, labels in node.props:
        if pname == name:
            out.append(parts)
    return out


def _has(node, name):
    return any(p == name for p, _, _ in node.props)


def _strings(node, name):
    """取属性的所有字符串值。"""
    out = []
    for parts in _prop(node, name):
        for kind, val in parts:
            if kind == 'str':
                out.append(val)
    return out


def _first_str(node, name, default=''):
    s = _strings(node, name)
    return s[0] if s else default


def _cells(node, name):
    """取属性的 cell 整数值（忽略引用）。"""
    out = []
    for parts in _prop(node, name):
        for kind, val in parts:
            if kind == 'cells':
                for c in val:
                    if isinstance(c, int):
                        out.append(c & 0xFFFFFFFF)
    return out


def _compat(node):
    return _strings(node, 'compatible')


def _status(node):
    st = _first_str(node, 'status', 'okay')
    return st or 'okay'


def _node_paths(root):
    """返回 [(path, node), ...] 以及 label->node, phandle->node, node->parent。"""
    result = []
    labels = {}
    phandles = {}
    parents = {}

    def walk(node, path, parent=None):
        result.append((path, node))
        parents[id(node)] = parent
        for lab in node.labels:
            labels[lab] = node
        for pname, parts, labs in node.props:
            for lab in labs:
                labels[lab] = (node, pname)
            if pname in ('phandle', 'linux,phandle'):
                for kind, val in parts:
                    if kind == 'cells':
                        for c in val:
                            if isinstance(c, int):
                                phandles[c & 0xFFFFFFFF] = node
        for ch in node.children:
            if ch.name in node.deleted_children:
                continue
            walk(ch, (path.rstrip('/') + '/' + ch.name) if path != '/' else '/' + ch.name, node)

    walk(root, '/')
    return result, labels, phandles, parents


def _fmt_hz(v):
    if v is None:
        return ''
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    if v >= 1_000_000_000:
        return '%.2f GHz' % (v / 1_000_000_000.0)
    if v >= 1_000_000:
        return '%.2f MHz' % (v / 1_000_000.0)
    if v >= 1_000:
        return '%.2f kHz' % (v / 1_000.0)
    return '%d Hz' % v


def _fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1024 ** 3:
        return '%.2f GB' % (n / float(1024 ** 3))
    if n >= 1024 ** 2:
        return '%.1f MB' % (n / float(1024 ** 2))
    if n >= 1024:
        return '%.1f KB' % (n / float(1024))
    return '%d B' % n


def _hex(v):
    return '0x%X' % (v & 0xFFFFFFFFFFFFFFFF)


def _reg_addr_size(node, addr_cells=2, size_cells=1):
    """解析 reg 属性为 [(addr,size),...]。"""
    cells = _cells(node, 'reg')
    step = addr_cells + size_cells
    out = []
    if step <= 0:
        return out
    for i in range(0, len(cells) - step + 1, step):
        grp = cells[i:i + step]
        addr = 0
        for c in grp[:addr_cells]:
            addr = (addr << 32) | (c & 0xFFFFFFFF)
        size = 0
        for c in grp[addr_cells:]:
            size = (size << 32) | (c & 0xFFFFFFFF)
        out.append((addr, size))
    return out


# ---------------------------------------------------------------------------
# 外设分类规则
# ---------------------------------------------------------------------------
# (类别, [compatible 关键词, 节点名关键词]) —— 小写匹配

_PERIPHERAL_RULES = [
    ('内存',              ['memory', '^memory', 'ram']),
    ('CPU / 处理器',      ['cortex', 'arm,arm', 'riscv', 'cpu']),
    ('串口 UART',         [' uart', 'ns16550', 'ns16', 'pl011', 'uart-', 'serial']),
    ('I2C 控制器',        ['i2c', 'i2c-', 'designware,i2c']),
    ('SPI 控制器',        ['spi', 'spi-', 'spi-nor', 'spi-nand']),
    ('MMC / SD / eMMC',   ['mmc', 'sdio', 'sdhc', 'sd-card', 'emmc']),
    ('USB 控制器',        ['usb', 'ohci', 'ehci', 'xhci', 'dwc2', 'dwc3', 'dwc-otg', 'type-c', 'typec']),
    ('以太网',            ['ethernet', 'gmac', 'emac', 'eth', 'stmmac', 'mac']),
    ('WiFi / 无线',       ['wifi', 'wlan', 'wireless', 'rtl8', 'rtl87', 'bcm43', 'sdio-wifi', 'ap6xxx']),
    ('蓝牙',              ['bluetooth', 'bluetooth-rfkill', 'bt,', 'uart-bt']),
    ('显示 / HDMI / LCD', ['hdmi', 'lcd', 'display', 'drm', 'vop', 'de,', 'framebuffer', 'panel', 'rgb']),
    ('GPU / 多媒体',      ['gpu', 'mali', 'vgpu', 'vpu', 'video-codec', 'cedar', 'hantro', 'jpeg', 'isp']),
    ('音频 / 声卡',       ['audio', 'sound', 'codec', 'i2s', 'spdif', 'dai', 'pdm', 'ac97']),
    (' GPIO 控制器',      ['gpio', 'gpio-controller', 'pinctrl']),
    ('PWM',               ['pwm']),
    ('ADC / 传感器',      ['adc', 'saradc', 'temperature', 'thermal', 'tsensor', 'sensor', 'iio']),
    ('触控 / 按键',       ['touchscreen', 'touch', 'goodix', 'ft5x', 'gt9', 'keys', 'gpio-keys', 'button']),
    ('LED',               ['led', 'leds', 'gpio-leds']),
    ('时钟 / 复位',       ['clock', 'clk', 'osc', 'pll', 'reset', 'ccu']),
    ('中断控制器',        ['interrupt-controller', 'gic', 'intc', 'irqchip', 'irq']),
    ('DMA',               ['dma']),
    ('RTC / 看门狗 / 定时器', ['rtc', 'watchdog', 'wdog', 'timer', 'pit']),
    ('电源 / 稳压 / 电池', ['regulator', 'pmic', 'battery', 'charger', 'power', 'axp', 'fel', 'sys_config']),
    ('加密 / 安全',       ['crypto', 'ce,', 'security', 'trustzone', 'tee']),
    ('存储 / 闪存',       ['nand', 'nor', 'flash', 'ata', 'sata', 'nvme', 'ufshcd']),
    ('CAN / 其他总线',    ['can', 'canfd', 'i3c']),
]


def _classify(node):
    compats = [c.lower() for c in _compat(node)]
    name = node.name.lower()
    for cat, keys in _PERIPHERAL_RULES:
        blob = ' '.join(compats) + ' ' + name
        for k in keys:
            k = k.strip().lower()
            if not k:
                continue
            # 关键词前后允许非字母数字，避免子串误匹配（例如 "spi" 命中 "risp"）
            if re.search(r'(?<![a-z0-9])' + re.escape(k) + r'(?![a-z0-9])', blob):
                return cat
    return '其他'


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------

def analyze_tree(root, reserves=None):
    """分析解析好的节点树，返回结构化结果 dict。"""
    reserves = reserves or []
    paths, labels, phandles, parents = _node_paths(root)
    nodes = [n for _, n in paths]

    def inherited_cells(node, key, fallback):
        """沿父节点向上查找 #address-cells / #size-cells。"""
        cur = node
        while cur is not None:
            vals = _cells(cur, key)
            if vals:
                return vals[0]
            cur = parents.get(id(cur))
        return fallback

    def _format_reg(node, max_ranges=2):
        """把 reg 解析为可读的地址(含大小), 处理 1/2/4 address-cells。"""
        cells = _cells(node, 'reg')
        if not cells:
            return ''
        ac = inherited_cells(node, '#address-cells', None)
        sc = inherited_cells(node, '#size-cells', None)
        n = len(cells)

        def try_parse(a, s):
            if a + s <= 0:
                return []
            return _reg_addr_size(node, a, s)

        parsed = []
        if ac is not None and sc is not None:
            parsed = try_parse(ac, sc)
        if not parsed or all(a == 0 and sz == 0 for a, sz in parsed):
            # 推断: 优先能被 cell 数整除且地址非零的组合
            candidates = []
            for a, s in ((1, 1), (2, 2), (2, 1), (1, 2), (4, 2)):
                if (a + s) > 0 and n % (a + s) == 0:
                    p = try_parse(a, s)
                    if p:
                        nonzero = sum(1 for addr, _ in p if addr != 0)
                        candidates.append((nonzero, a, s, p))
            if candidates:
                candidates.sort(reverse=True)
                parsed = candidates[0][3]
        if not parsed:
            # 兜底: 整个 reg 当成一个大地址
            v = 0
            for c in cells:
                v = (v << 32) | (c & 0xFFFFFFFF)
            return _hex(v)

        parts = []
        for addr, size in parsed[:max_ranges]:
            s = _hex(addr)
            if size:
                s += '+' + _hex(size)
            parts.append(s)
        out = ', '.join(parts)
        if len(parsed) > max_ranges:
            out += '…'
        return out

    result = {
        'board': {},
        'summary': {},
        'sections': [],
        'warnings': [],
    }

    # ---- 板卡 / SoC / CPU ----
    board = {
        'model': _first_str(root, 'model', ''),
        'compatible': _compat(root),
        'vendor': '',
        'soc': '',
        'cpu': '',
        'cpu_freqs': [],
    }
    soc = ''
    vendor = ''
    soc_matchers = [
        ('Allwinner 全志', ['allwinner,sun']),
        ('Rockchip 瑞芯微', ['rockchip,rk']),
        ('Amlogic 晶晨',   ['amlogic,']),
        ('Actions 炬芯',    ['actions,']),
        ('Sunplus 凌阳',    ['sunplus,']),
        ('NXP 恩智浦',      ['freescale,', 'nxp,']),
        ('TI 德州仪器',     ['ti,']),
        ('ST 意法半导体',   ['st,']),
        ('Broadcom 博通',   ['brcm,bcm', 'broadcom,']),
        ('Marvell迈威',     ['marvell,']),
        ('Qualcomm 高通',   ['qcom,']),
        ('Intel 英特尔',    ['intel,']),
    ]
    joined = ' '.join(c.lower() for c in board['compatible'])
    for vname, keys in soc_matchers:
        for k in keys:
            if k in joined:
                vendor = vname
                m = re.search(re.escape(k) + r'[a-z0-9\-]+', joined)
                if m:
                    soc = m.group(0).rstrip('-')
                break
        if vendor:
            break
    # 优先取"芯片级" compatible(通常是末尾不含 -board/-evb 的 vendor,chip 项)
    chip_candidates = []
    for c in board['compatible']:
        parts = c.split(',')
        if len(parts) >= 2 and not any(x in c.lower() for x in ('-board', '-evb', '-lts', '-pi')):
            chip_candidates.append(c)
    if chip_candidates:
        board['soc'] = chip_candidates[-1]
    board['vendor'] = vendor
    soc = board['soc']
    if not soc and board['compatible']:
        soc = board['compatible'][0]
        board['soc'] = soc

    # CPU
    cpus = [n for n in nodes if _first_str(n, 'device_type') == 'cpu'
            or _classify(n) == 'CPU / 处理器']
    cpu_desc = []
    cpu_freqs = []
    for c in cpus:
        cc = _compat(c)
        desc = cc[0] if cc else c.name
        cpu_desc.append(desc)
        for f in _cells(c, 'clock-frequency'):
            cpu_freqs.append(f)
    board['cpu'] = ', '.join(cpu_desc)
    board['cpu_freqs'] = cpu_freqs
    result['board'] = board

    # ---- 内存 ----
    mem_nodes = [n for n in nodes if n.name.lower().startswith('memory')]
    mem_rows = []
    for m in mem_nodes:
        ncell = len(_cells(m, 'reg'))
        # 若父链上没有显式声明，按 reg cell 数推断常见组合
        ac = inherited_cells(m, '#address-cells', None)
        sc = inherited_cells(m, '#size-cells', None)
        if ac is None or sc is None:
            if ncell % 2 == 0 and ncell // 2 in (1, 2, 4):
                ac = sc = ncell // 2
            else:
                ac, sc = (ac if ac is not None else 1), (sc if sc is not None else 1)
        parsed = _reg_addr_size(m, ac, sc)
        # 若解析出的 size 全为 0，尝试交换/其他组合
        if parsed and all(s == 0 for _, s in parsed):
            for ac2, sc2 in ((1, 1), (2, 2), (1, 2), (2, 1)):
                alt = _reg_addr_size(m, ac2, sc2)
                if alt and any(s > 0 for _, s in alt):
                    parsed = alt
                    break
        for addr, size in parsed:
            mem_rows.append([m.name, _hex(addr), _fmt_size(size), _hex(size)])
    for addr, size in reserves:
        mem_rows.append(['/memreserve/', _hex(addr), _fmt_size(size), _hex(size)])
    if mem_rows:
        result['sections'].append({
            'title': '内存 / 保留区',
            'cols': ['节点', '基地址', '容量', '字节数'],
            'rows': mem_rows,
        })

    # ---- 板卡概览 section ----
    ov = []
    if board['model']:
        ov.append(['型号 model', board['model']])
    if board['vendor']:
        ov.append(['厂商', board['vendor']])
    if board['soc']:
        ov.append(['SoC', board['soc']])
    if board['compatible']:
        ov.append(['compatible', ', '.join(board['compatible'][:4])])
    if board['cpu']:
        ov.append(['CPU', board['cpu']])
    if cpu_freqs:
        ov.append(['CPU 频率', ', '.join(_fmt_hz(f) for f in cpu_freqs)])
    result['sections'].append({
        'title': '板卡 / SoC',
        'cols': ['项目', '值'],
        'rows': ov,
    })

    # ---- 外设清单 ----
    periph_groups = {}
    enabled = disabled = 0
    for path, node in paths:
        if node.name == '/':
            continue
        if node.name.lower().startswith('memory'):  # 已单独列在"内存/保留区"
            continue
        if not _compat(node) and not _has(node, 'device_type'):
            continue
        cat = _classify(node)
        st = _status(node)
        if st == 'disabled':
            disabled += 1
        else:
            enabled += 1
        compats = _compat(node)
        cstr = compats[0] if compats else (_first_str(node, 'device_type') or '-')
        # 地址: 用继承的 #address-cells/#size-cells 解析完整寄存器地址(而非第一个 cell)
        addr = _format_reg(node, max_ranges=2)
        # 补充关键属性（频率、状态等）
        extra = []
        if st != 'okay':
            extra.append('状态=' + st)
        for pn in ('clock-frequency', 'max-frequency', 'assigned-clock-rates',
                   'bus-width', 'clock-names', 'interrupts'):
            vals = _cells(node, pn)
            strs = _strings(node, pn)
            if pn in ('clock-names',):
                if strs:
                    extra.append(pn + '=' + ','.join(strs[:3]))
            elif vals:
                if 'frequency' in pn or 'rates' in pn:
                    extra.append(pn + '=' + ','.join(_fmt_hz(v) for v in vals[:2]))
                else:
                    extra.append(pn + '=' + ','.join(_hex(v) for v in vals[:3]))
        periph_groups.setdefault(cat, []).append([
            node.name, cstr, st, addr, '; '.join(extra)
        ])

    for cat in sorted(periph_groups.keys()):
        result['sections'].append({
            'title': '外设 · ' + cat,
            'cols': ['节点', 'compatible', '状态', '寄存器', '关键参数'],
            'rows': periph_groups[cat],
        })

    # ---- GPIO 引脚使用 ----
    gpio_rows = []
    for path, node in paths:
        for pname, parts, labs in node.props:
            # 匹配 gpios / xxx-gpios / gpio / gpio-line-names / reset-gpios 等
            if not (pname == 'gpios' or pname.endswith('-gpios') or pname == 'gpio'):
                continue
            for kind, val in parts:
                if kind != 'cells' or not val:
                    continue
                # 常见格式: <&gpioX pin flags> 或 <phandle pin flags>
                ctrl = val[0]
                pin = val[1] if len(val) > 1 else None
                flags = val[2:] if len(val) > 2 else []
                ctrl_name = ''
                if isinstance(ctrl, E.Ref):
                    ctrl_name = ctrl.text
                    target = labels.get(ctrl.text.lstrip('&'))
                    if isinstance(target, E.Node):
                        ctrl_name += ' (' + target.name + ')'
                elif isinstance(ctrl, int):
                    tgt = phandles.get(ctrl & 0xFFFFFFFF)
                    ctrl_name = ('phandle=0x%X' % ctrl) + ((' (' + tgt.name + ')') if tgt else '')
                flag_desc = []
                for f in flags:
                    f = f & 0xFFFFFFFF if isinstance(f, int) else f
                    if f == 1:
                        flag_desc.append('低有效')
                    elif f == 0:
                        flag_desc.append('高有效')
                    else:
                        flag_desc.append(_hex(f))
                gpio_rows.append([
                    node.name, pname, ctrl_name,
                    str(pin) if pin is not None else '',
                    ','.join(flag_desc),
                ])
    # GPIO 控制器（提供哪些 bank）
    for path, node in paths:
        if _has(node, 'gpio-controller'):
            ngpio = _cells(node, '#gpio-cells')
            gpio_rows.append([node.name, 'gpio-controller',
                              'bank', ('cells=' + str(ngpio[0])) if ngpio else '', ''])
    if gpio_rows:
        result['sections'].append({
            'title': 'GPIO 引脚使用',
            'cols': ['节点', '属性', '控制器 / Bank', '引脚', '标志'],
            'rows': gpio_rows,
        })

    # ---- pinctrl 引脚复用 ----
    pinctrl_rows = []
    for path, node in paths:
        # pinctrl 状态节点通常在其名字/compatible 里带 pin 功能描述
        pin_props = []
        for pname, parts, labs in node.props:
            pl = pname.lower()
            if pl in ('pins', 'function', 'groups') or pl.endswith('-pins') \
                    or pl.endswith('-functions') or pl.endswith('-groups') \
                    or 'pinmux' in pl or 'pinctrl' in pl:
                vals = _strings(node, pname) + [str(v) for v in _cells(node, pname)]
                if vals:
                    pin_props.append((pname, vals[:12]))
        func = _first_str(node, 'function', '')
        if pin_props or func:
            for pname, vals in pin_props:
                pinctrl_rows.append([node.name, pname, ', '.join(str(v) for v in vals)])
            if func and not any(p == 'function' for p, _ in pin_props):
                pinctrl_rows.append([node.name, 'function', func])
    if pinctrl_rows:
        result['sections'].append({
            'title': '引脚复用 (Pinctrl)',
            'cols': ['节点 / 状态', '属性', '引脚 / 功能'],
            'rows': pinctrl_rows,
        })

    # ---- 时钟 / 频率 ----
    clock_rows = []
    seen = set()
    for path, node in paths:
        for pn in ('clock-frequency', 'assigned-clock-rates', 'clock-rate',
                   'max-frequency', 'bus-frequency', 'clock-frequency-high'):
            vals = _cells(node, pn)
            for v in vals:
                key = (node.name, pn, v)
                if key in seen:
                    continue
                seen.add(key)
                clock_rows.append([node.name, pn, _fmt_hz(v), _hex(v)])
    if clock_rows:
        result['sections'].append({
            'title': '时钟 / 频率',
            'cols': ['节点', '属性', '频率', '原始值'],
            'rows': clock_rows,
        })

    # ---- 中断 ----
    irq_rows = []
    for path, node in paths:
        irqs = _cells(node, 'interrupts')
        names = _strings(node, 'interrupt-names')
        parents = _cells(node, 'interrupt-parent')
        if irqs or names:
            irq_desc = ', '.join(_hex(v) for v in irqs[:6])
            irq_rows.append([node.name, irq_desc, ','.join(names[:4]),
                             ('parent=0x%X' % (parents[0] & 0xFFFFFFFF)) if parents else ''])
    if irq_rows:
        result['sections'].append({
            'title': '中断',
            'cols': ['节点', 'interrupts', '名称', '父控制器'],
            'rows': irq_rows,
        })

    # ---- 汇总 ----
    result['summary'] = {
        'nodes': len(nodes),
        'properties': sum(len(n.props) for n in nodes),
        'peripherals': enabled + disabled,
        'enabled': enabled,
        'disabled': disabled,
        'sections': len(result['sections']),
    }
    return result


def analyze_text(text, include_dirs=None, preprocess=False):
    """分析 DTS 文本。preprocess=True 时先展开 #include/#define。"""
    include_dirs = list(include_dirs or [])
    warnings = []
    if preprocess:
        text, warnings = E.preprocess_dts(text, include_dirs=include_dirs)
    parser = E.Parser(text)
    root = parser.parse()
    result = analyze_tree(root, parser.reserves)
    result['warnings'] = warnings + result.get('warnings', [])
    return result


def analyze_dtb(data):
    """分析 DTB 字节。"""
    if isinstance(data, (bytes, bytearray)):
        text = E.dtb_to_dts(bytes(data))
    else:
        raise TypeError('data 必须是 bytes')
    return analyze_text(text, preprocess=False)


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------

def report_text(result):
    """把结构化分析结果渲染为纯文本报告（CLI 用）。"""
    L = []
    L.append('=' * 60)
    L.append('DTS 硬件分析报告')
    L.append('=' * 60)
    b = result.get('board', {})
    if b.get('model'):
        L.append('型号    : %s' % b['model'])
    if b.get('vendor') or b.get('soc'):
        L.append('SoC     : %s %s' % (b.get('vendor', ''), b.get('soc', '')))
    if b.get('cpu'):
        L.append('CPU     : %s' % b['cpu'])
    if b.get('cpu_freqs'):
        L.append('CPU 频率: %s' % ', '.join(_fmt_hz(f) for f in b['cpu_freqs']))
    if b.get('compatible'):
        L.append('兼容    : %s' % ', '.join(b['compatible'][:6]))
    s = result.get('summary', {})
    L.append('节点/属性: %d / %d   外设: %d (启用 %d, 禁用 %d)'
             % (s.get('nodes', 0), s.get('properties', 0), s.get('peripherals', 0),
                s.get('enabled', 0), s.get('disabled', 0)))
    for sec in result.get('sections', []):
        L.append('')
        L.append('【%s】' % sec['title'])
        cols = sec['cols']
        rows = sec['rows']
        if not rows:
            L.append('  (无)')
            continue
        # 简单列宽对齐
        widths = []
        for ci in range(len(cols)):
            w = len(cols[ci])
            for r in rows:
                if ci < len(r):
                    w = max(w, len(str(r[ci])))
            widths.append(min(w, 42))
        def fmtrow(vals):
            cells = []
            for ci, w in enumerate(widths):
                v = str(vals[ci]) if ci < len(vals) else ''
                if len(v) > w:
                    v = v[:w - 1] + '…'
                cells.append(v.ljust(w))
            return '  ' + '  '.join(cells).rstrip()
        L.append(fmtrow(cols))
        L.append('  ' + '-' * (sum(widths) + 2 * (len(widths) - 1)))
        for r in rows:
            L.append(fmtrow(r))
    if result.get('warnings'):
        L.append('')
        L.append('【警告】')
        for w in result['warnings']:
            L.append('  ! ' + w)
    return '\n'.join(L)
