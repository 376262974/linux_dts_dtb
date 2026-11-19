# -*- coding: utf-8 -*-
"""
dtb_engine.py - 纯 Python 的 DTB <-> DTS 转换引擎，无任何外部依赖。

设计取舍:
  * 目标工作流是「反编译 -> 编辑 DTS -> 编译回去」，因此编译器覆盖
    dtc 反编译输出的标准 DTS 语法以及手写的常见 DTS：
      节点 / 属性 / 字符串 / 单元格数组 / 字节数组 / 表达式 /
      label 引用(&label) / 路径引用(&{/path}) / /delete-* / /memreserve/
  * 不支持 C 预处理器(#include / #define / 宏)，这类复杂源文件请用真正的 dtc。
"""

import re
import struct
from pathlib import Path

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 0x00000001
FDT_END_NODE = 0x00000002
FDT_PROP = 0x00000003
FDT_NOP = 0x00000004
FDT_END = 0x00000009


def align4(x):
    return (x + 3) & ~3


# ===========================================================================
# DTB -> DTS
# ===========================================================================

def _escape_c(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append('\\x%02x' % (o & 0xFF))
    return ''.join(out)


def _looks_like_string(raw):
    if not raw.endswith(b'\0'):
        return None
    parts = raw.split(b'\0')
    # 最后一个元素为空（因为以 NUL 结尾），其余不得含 NUL（split 已保证）
    pieces = parts[:-1]
    if not pieces or all(len(p) == 0 for p in pieces):
        return None  # 全 NUL(如 <0x00>)不是字符串
    decoded = []
    for p in pieces:
        try:
            s = p.decode('utf-8')
        except UnicodeDecodeError:
            return None
        for ch in s:
            o = ord(ch)
            if not (32 <= o < 127 or ch in '\t\n\r'):
                return None
        decoded.append(s)
    return decoded


def format_value(raw):
    """把属性原始字节格式化为 DTS 值文本。"""
    if len(raw) == 0:
        return None  # 空属性 -> `name;`
    strings = _looks_like_string(raw)
    if strings is not None:
        return ', '.join('"%s"' % _escape_c(s) for s in strings)
    if len(raw) % 4 == 0:
        cells = struct.unpack('>%dI' % (len(raw) // 4), raw)
        return '<%s>' % ' '.join('0x%02x' % c for c in cells)
    return '[%s]' % ' '.join('%02x' % b for b in raw)


def dtb_to_dts(data):
    """把 DTB 字节转换为 DTS 文本。"""
    if len(data) < 32:
        raise ValueError('文件过小，不是有效 DTB')
    magic, total, off_struct, off_strings, off_rsvmap, version = struct.unpack('>6I', data[:24])
    if magic != FDT_MAGIC:
        raise ValueError('不是有效的 DTB (magic=0x%08X)' % magic)

    size_struct = total - off_struct
    if version >= 17 and len(data) >= 40:
        size_struct = struct.unpack('>I', data[36:40])[0]
    end = min(off_struct + size_struct, len(data))

    lines = ['/dts-v1/;', '']
    # 保留内存区
    pos = off_rsvmap
    while pos + 16 <= len(data):
        addr, size = struct.unpack('>QQ', data[pos:pos + 16])
        pos += 16
        if addr == 0 and size == 0:
            break
        lines.append('/memreserve/ 0x%016x 0x%016x;' % (addr, size))
    if len(lines) > 2:
        lines.append('')

    stack = []
    pos = off_struct
    while pos + 4 <= end:
        tok = struct.unpack('>I', data[pos:pos + 4])[0]
        pos += 4
        if tok == FDT_BEGIN_NODE:
            z = data.find(b'\0', pos, end)
            if z < 0:
                break
            name = data[pos:z].decode('utf-8', 'replace')
            pos = align4(z + 1)
            indent = '    ' * len(stack)
            lines.append('%s%s {' % (indent, name if name else '/'))
            stack.append(name)
        elif tok == FDT_END_NODE:
            if stack:
                stack.pop()
            lines.append('%s};' % ('    ' * len(stack)))
        elif tok == FDT_PROP:
            if pos + 8 > end:
                break
            length, nameoff = struct.unpack('>II', data[pos:pos + 8])
            pos += 8
            zs = data.find(b'\0', off_strings + nameoff)
            if zs < 0:
                zs = off_strings + nameoff
            pname = data[off_strings + nameoff:zs].decode('utf-8', 'replace')
            raw = data[pos:pos + length]
            pos = align4(pos + length)
            indent = '    ' * len(stack)
            val = format_value(raw)
            if val is None:
                lines.append('%s%s;' % (indent, pname))
            else:
                lines.append('%s%s = %s;' % (indent, pname, val))
        elif tok == FDT_NOP:
            continue
        elif tok == FDT_END:
            break
        else:
            break

    while len(stack) > 1:
        stack.pop()
        lines.append('%s};' % ('    ' * len(stack)))
    return '\n'.join(lines) + '\n'


# ===========================================================================
# DTS -> DTB  (词法 + 语法分析 + 生成)
# ===========================================================================

class DtsError(Exception):
    pass


class Ref:
    """label 或路径引用，编译期解析为 phandle 数值。"""
    __slots__ = ('text',)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return 'Ref(%s)' % self.text


class Node:
    def __init__(self, name='/'):
        self.name = name
        self.props = []          # (name, parts, labels)
        self.children = []
        self.labels = []
        self.deleted_props = set()
        self.deleted_children = set()


_DIRECTIVE = re.compile(r'/[A-Za-z][\w-]*/')
_NUMBER = re.compile(r'0[xX][0-9a-fA-F]+|\d+')
# 名字允许: 字母/下划线开头，可含 数字 . , - + # @ ? ；也允许 # 开头(如 #address-cells)
_NAME = re.compile(r'[#A-Za-z_][A-Za-z0-9_,.\-+#@?]*')
_MULTIOP = ('<<', '>>', '<=', '>=', '==', '!=')


def tokenize(text):
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        m = _DIRECTIVE.match(text, i)
        if m:
            toks.append(('DIR', m.group()))
            i = m.end()
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == '\\' and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r', '0': '\0',
                                'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v'}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            toks.append(('STR', ''.join(buf)))
            i = j + 1
            continue
        if c == '&':
            if i + 1 < n and text[i + 1] == '{':
                j = text.find('}', i + 2)
                if j >= 0:
                    toks.append(('REF', text[i + 1:j + 1]))
                    i = j + 1
                    continue
            m = re.match(r'&([A-Za-z_]\w*)', text[i:])
            if m:
                toks.append(('REF', '&' + m.group(1)))
                i += len(m.group())
                continue
            toks.append(('SYM', '&'))
            i += 1
            continue
        m = _NUMBER.match(text, i)
        if m:
            toks.append(('NUM', m.group()))
            i = m.end()
            continue
        m = _NAME.match(text, i)
        if m:
            toks.append(('NAME', m.group()))
            i = m.end()
            continue
        matched = False
        for op in _MULTIOP:
            if text.startswith(op, i):
                toks.append(('SYM', op))
                i += len(op)
                matched = True
                break
        if matched:
            continue
        toks.append(('SYM', c))
        i += 1
    toks.append(('EOF', ''))
    return toks


class Parser:
    def __init__(self, text):
        self.toks = tokenize(text)
        self.i = 0
        self.reserves = []
        self.root = None

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ('EOF', '')

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def sym(self, s):
        t = self.peek()
        return t[1] == s and t[0] in ('SYM', 'DIR', 'EOF', 'NAME', 'NUM', 'STR', 'REF')

    def expect_sym(self, s):
        if not self.sym(s):
            raise DtsError('期望 "%s"，实际得到 "%s"(第 %d 个 token)' % (s, self.peek()[1], self.i))
        self.next()

    def expect_name(self):
        t = self.next()
        if t[0] != 'NAME':
            raise DtsError('期望名称，实际得到 "%s"' % t[1])
        return t[1]

    def parse(self):
        if self.peek()[0] == 'DIR' and self.peek()[1] == '/dts-v1/':
            self.next()
            self.expect_sym(';')
        while self.peek()[0] != 'EOF':
            t = self.peek()
            if t[0] == 'DIR' and t[1] == '/memreserve/':
                self.next()
                a = self._number()
                s = self._number()
                self.expect_sym(';')
                self.reserves.append((a, s))
            elif t[0] == 'DIR' and t[1] == '/plugin/':
                self.next()
                self.expect_sym(';')
            elif self.sym('/') and self.peek(1)[1] == '{':
                self.next()  # '/'
                self.next()  # '{'
                self.root = self.parse_body(Node('/'))
                if self.sym(';'):
                    self.next()
            else:
                raise DtsError('顶层出现无法识别的内容: "%s"' % t[1])
        if self.root is None:
            raise DtsError('没有找到根节点 / { ... };')
        return self.root

    def _number(self):
        t = self.next()
        if t[0] != 'NUM':
            raise DtsError('期望数字，实际得到 "%s"' % t[1])
        s = t[1]
        return int(s, 16) if s.lower().startswith('0x') else int(s, 10)

    def parse_body(self, node):
        while True:
            t = self.peek()
            if t[0] == 'EOF':
                raise DtsError('节点 %s 缺少结束符 }' % node.name)
            if t[1] == '}':
                self.next()
                if self.sym(';'):   # 消费节点结束的 ';'
                    self.next()
                return node
            # label: 前缀
            labels = []
            while self.peek()[0] == 'NAME' and self.peek(1)[1] == ':':
                labels.append(self.next()[1])
                self.next()
            if self.peek()[0] == 'DIR' and self.peek()[1] == '/delete-property/':
                self.next()
                node.deleted_props.add(self.expect_name())
                self.expect_sym(';')
                continue
            if self.peek()[0] == 'DIR' and self.peek()[1] == '/delete-node/':
                self.next()
                node.deleted_children.add(self.expect_name())
                self.expect_sym(';')
                continue
            name = self.expect_name()
            nt = self.peek()
            if nt[1] == '{':
                self.next()
                child = self.parse_body(Node(name))
                child.labels = labels
                node.children.append(child)
            elif nt[1] == '=':
                self.next()
                parts = self.parse_value()
                self.expect_sym(';')
                node.props.append((name, parts, labels))
            elif nt[1] == ';':
                self.next()
                node.props.append((name, [], labels))
            else:
                raise DtsError('节点/属性 "%s" 后出现无法识别的 "%s"' % (name, nt[1]))
        return node

    def parse_value(self):
        parts = []
        while True:
            t = self.peek()
            if t[0] == 'STR':
                parts.append(('str', self.next()[1]))
            elif t[1] == '<':
                self.next()
                parts.append(('cells', self.parse_cells()))
            elif t[1] == '[':
                self.next()
                parts.append(('bytes', self.parse_bytes()))
            elif t[0] == 'REF':
                parts.append(('ref', Ref(self.next()[1])))
            elif t[1] == ',':
                self.next()
            else:
                break
        if not parts:
            raise DtsError('属性 = 之后没有值')
        return parts

    def parse_cells(self):
        cells = []
        while True:
            t = self.peek()
            if t[1] == '>':
                self.next()
                break
            if t[0] == 'EOF':
                raise DtsError('单元格数组缺少 >')
            cells.append(self.parse_expr())
        return cells

    def parse_bytes(self):
        out = []
        while True:
            t = self.peek()
            if t[1] == ']':
                self.next()
                break
            if t[0] == 'NUM':
                self.next()
                v = int(t[1], 16) if t[1].lower().startswith('0x') else int(t[1], 10)
                if v < 0 or v > 255:
                    raise DtsError('字节值超出范围: %d' % v)
                out.append(v)
            elif t[0] == 'NAME' and re.fullmatch(r'[0-9a-fA-F]{1,2}', t[1]):
                # 字节数组中的裸十六进制，如 [00 11 ff]
                self.next()
                out.append(int(t[1], 16))
            elif t[0] == 'STR':
                s = self.next()[1]
                out.extend(s.encode('latin-1', 'replace'))
            else:
                raise DtsError('字节数组中出现无法识别的 "%s"' % t[1])
        return out

    # ---- 表达式 (递归下降) ----
    def parse_expr(self):
        return self._expr_or()

    def _binary(self, sub, ops):
        v = sub()
        while self.peek()[1] in ops:
            op = self.next()[1]
            r = sub()
            if op in ('+', '-', '*', '/', '%', '<<', '>>', '&', '|', '^'):
                if op == '+': v += r
                elif op == '-': v -= r
                elif op == '*': v *= r
                elif op == '/': v = int(v / r) if r else 0
                elif op == '%': v = v % r if r else 0
                elif op == '<<': v <<= r
                elif op == '>>': v >>= r
                elif op == '&': v &= r
                elif op == '|': v |= r
                elif op == '^': v ^= r
            elif op in ('<', '>', '<=', '>=', '==', '!='):
                v = int({'<': v < r, '>': v > r, '<=': v <= r,
                         '>=': v >= r, '==': v == r, '!=': v != r}[op])
        return v

    def _expr_or(self):
        return self._binary(self._expr_xor, ('|',))

    def _expr_xor(self):
        return self._binary(self._expr_and, ('^',))

    def _expr_and(self):
        return self._binary(self._expr_shift, ('&',))

    def _expr_shift(self):
        return self._binary(self._expr_add, ('<<', '>>'))

    def _expr_add(self):
        return self._binary(self._expr_mul, ('+', '-'))

    def _expr_mul(self):
        return self._binary(self._expr_unary, ('*', '/', '%'))

    def _expr_unary(self):
        t = self.peek()
        if t[1] == '~':
            self.next()
            return ~self._expr_unary()
        if t[1] == '!':
            self.next()
            return int(not self._expr_unary())
        if t[1] == '-':
            self.next()
            return -self._expr_unary()
        if t[1] == '+':
            self.next()
            return self._expr_unary()
        return self._expr_primary()

    def _expr_primary(self):
        t = self.next()
        if t[0] == 'NUM':
            s = t[1]
            return int(s, 16) if s.lower().startswith('0x') else int(s, 10)
        if t[1] == '(':
            v = self.parse_expr()
            self.expect_sym(')')
            return v
        if t[0] == 'REF':
            return Ref(t[1])
        raise DtsError('表达式中出现无法识别的 "%s"' % t[1])


class StringsBlock:
    def __init__(self):
        self.data = bytearray()
        self.offsets = {}

    def offset(self, name):
        if name not in self.offsets:
            self.offsets[name] = len(self.data)
            self.data.extend(name.encode('utf-8'))
            self.data.append(0)
        return self.offsets[name]


def _collect(root):
    paths = {}
    labels = {}
    existing = set()

    def walk(node, path):
        paths[path] = node
        for lab in node.labels:
            labels[lab] = node
        for name, parts, labs in node.props:
            for lab in labs:
                labels[lab] = (node, name)
            for kind, val in parts:
                seq = val if kind in ('cells', 'bytes') else [val] if kind == 'ref' else []
                if kind == 'cells' or kind == 'ref':
                    for c in seq:
                        if isinstance(c, int) and name == 'phandle':
                            existing.add(c & 0xFFFFFFFF)
        for ch in node.children:
            if ch.name in node.deleted_children:
                continue
            walk(ch, (path.rstrip('/') + '/' + ch.name) if path != '/' else '/' + ch.name)

    walk(root, '/')
    return paths, labels, existing


def _resolve_refs(root, paths, labels, existing):
    """为被引用但尚无 phandle 的节点分配 phandle，并注入 phandle 属性。"""
    next_handle = [1]

    def fresh():
        while next_handle[0] in existing:
            next_handle[0] += 1
        v = next_handle[0]
        next_handle[0] += 1
        existing.add(v)
        return v

    def ref_node(ref):
        text = ref.text
        if text.startswith('{'):          # &{/path} -> tokenizer 存为 "{/path}"
            return paths.get(text[1:-1])
        if text.startswith('/'):
            return paths.get(text)
        label = text[1:]
        target = labels.get(label)
        if isinstance(target, Node):
            return target
        if isinstance(target, tuple) and isinstance(target[0], Node):
            return target[0]
        return None

    def handle(node):
        for name, parts, labs in node.props:
            if name != 'phandle':
                continue
            for kind, val in parts:
                if kind == 'cells':
                    for c in val:
                        if isinstance(c, int):
                            existing.add(c & 0xFFFFFFFF)
        for ch in node.children:
            handle(ch)

    handle(root)

    def scan(node):
        for name, parts, labs in node.props:
            for kind, val in parts:
                if kind == 'cells':
                    cells = val
                elif kind == 'ref':
                    cells = [val]
                else:
                    cells = []
                for c in cells:
                    if isinstance(c, Ref):
                        target = ref_node(c)
                        if target is None:
                            raise DtsError('无法解析引用 %s' % c.text)
                        if not any(p[0] == 'phandle' for p in target.props):
                            target.props.append(('phandle', [('cells', [fresh()])], []))
        for ch in node.children:
            scan(ch)

    scan(root)

    def resolve(c):
        if not isinstance(c, Ref):
            return c & 0xFFFFFFFF
        target = ref_node(c)
        for name, parts, labs in target.props:
            if name == 'phandle':
                for kind, val in parts:
                    if kind == 'cells' and val and isinstance(val[0], int):
                        return val[0] & 0xFFFFFFFF
        return 0

    return resolve


def _encode(parts, resolve):
    out = bytearray()
    for kind, val in parts:
        if kind == 'str':
            out.extend(val.encode('utf-8'))
            out.append(0)
        elif kind == 'bytes':
            out.extend(bytes(val))
        elif kind == 'cells':
            for c in val:
                out.extend(struct.pack('>I', resolve(c)))
        elif kind == 'ref':
            out.extend(struct.pack('>I', resolve(val)))
    return bytes(out)


def dts_to_dtb(text):
    """把 DTS 文本编译为 DTB 字节。"""
    parser = Parser(text)
    root = parser.parse()
    paths, labels, existing = _collect(root)
    resolve = _resolve_refs(root, paths, labels, existing)
    strings = StringsBlock()

    def emit(node):
        name = node.name if node.name != '/' else ''
        b = bytearray(struct.pack('>I', FDT_BEGIN_NODE))
        b.extend(name.encode('utf-8'))
        b.append(0)
        while len(b) % 4:
            b.append(0)
        for pname, parts, labs in node.props:
            if pname in node.deleted_props:
                continue
            raw = _encode(parts, resolve)
            b.extend(struct.pack('>III', FDT_PROP, len(raw), strings.offset(pname)))
            b.extend(raw)
            while len(b) % 4:
                b.append(0)
        for ch in node.children:
            if ch.name in node.deleted_children:
                continue
            b.extend(emit(ch))
        b.extend(struct.pack('>I', FDT_END_NODE))
        return b

    struct_block = emit(root)
    struct_block.extend(struct.pack('>I', FDT_END))
    rsv = bytearray()
    for addr, size in parser.reserves:
        rsv.extend(struct.pack('>QQ', addr & 0xFFFFFFFFFFFFFFFF, size & 0xFFFFFFFFFFFFFFFF))
    rsv.extend(struct.pack('>QQ', 0, 0))

    off_rsvmap = 40
    off_struct = off_rsvmap + len(rsv)
    off_strings = off_struct + len(struct_block)
    total = off_strings + len(strings.data)
    header = struct.pack('>10I', FDT_MAGIC, total, off_struct, off_strings,
                         off_rsvmap, 17, 16, 0, len(strings.data), len(struct_block))
    return bytes(header) + bytes(rsv) + bytes(struct_block) + bytes(strings.data)


# ===========================================================================
# DTS 预处理器  (#include / #define / 条件编译)
# ===========================================================================
#
# 让源码树风格、带预处理指令的 DTS 也能在纯 Python 环境下编译。
# 内置常见 dt-bindings 宏；SoC 专有头文件由用户通过 include_dirs 提供。

BUILTIN_HEADERS = {
    "dt-bindings/gpio/gpio.h": """
#define GPIO_ACTIVE_HIGH 0
#define GPIO_ACTIVE_LOW 1
#define GPIO_OPEN_DRAIN (1 << 2)
#define GPIO_OPEN_SOURCE (1 << 3)
#define GPIO_PERSISTENT (1 << 4)
#define GPIO_MAX_NAME_SIZE 32
""",
    "dt-bindings/interrupt-controller/irq.h": """
#define IRQ_TYPE_NONE 0
#define IRQ_TYPE_EDGE_RISING 1
#define IRQ_TYPE_EDGE_FALLING 2
#define IRQ_TYPE_EDGE_BOTH 3
#define IRQ_TYPE_LEVEL_HIGH 4
#define IRQ_TYPE_LEVEL_LOW 8
#define IRQ_TYPE_LEVEL_MASK (IRQ_TYPE_LEVEL_LOW | IRQ_TYPE_LEVEL_HIGH)
#define IRQ_TYPE_EDGE_BOTH (IRQ_TYPE_EDGE_FALLING | IRQ_TYPE_EDGE_RISING)
#define IRQ_TYPE_SENSE_MASK 0x0f
#define IRQ_TYPE_DEFAULT 3
#define IRQ_FLAGS_MASK 0x0f
""",
    "dt-bindings/interrupt-controller/arm-gic.h": """
#include <dt-bindings/interrupt-controller/irq.h>
#define GIC_SPI 0
#define GIC_PPI 1
#define GIC_FIQ 2
#define GIC_IRQ_TYPE_SIPI_SGI(val)  ((val) & 0xf)
""",
    "dt-bindings/input/input-event-codes.h": """
#define KEY_ENTER 28
#define KEY_ESC 1
#define KEY_SPACE 57
#define KEY_A 30
#define KEY_Z 44
#define KEY_0 11
#define KEY_9 2
#define BTN_LEFT 0x110
#define BTN_RIGHT 0x111
#define BTN_MIDDLE 0x112
#define EV_KEY 0x01
#define EV_REL 0x02
#define EV_ABS 0x03
#define EV_MSC 0x04
#define EV_LED 0x11
#define EV_SND 0x12
#define EV_REP 0x14
#define EV_FF 0x15
#define ABS_X 0x00
#define ABS_Y 0x01
#define ABS_Z 0x02
#define ABS_MT_SLOT 0x2f
#define ABS_MT_POSITION_X 0x35
#define ABS_MT_POSITION_Y 0x36
#define ABS_MT_TRACKING_ID 0x39
#define REL_X 0x00
#define REL_Y 0x01
#define SYN_REPORT 0
#define SYN_CONFIG 1
#define SYN_MT_REPORT 2
#define FF_MAX 0x7f
#define LED_MAX 0x0f
#define REP_DELAY 0
#define REP_PERIOD 1
""",
    "dt-bindings/thermal/thermal.h": """
#define THERMAL_NO_LIMIT 0
#define THERMAL_TRIP_CRITICAL 6
#define THERMAL_TRIP_HOT 5
#define THERMAL_TRIP_PASSIVE 4
#define THERMAL_TRIP_ACTIVE 3
#define THERMAL_TRIP_CONFIGURABLE_HOT 2
#define THERMAL_TRIP_CONFIGURABLE_CRITICAL 1
""",
    "dt-bindings/regulator/regulator.h": """
#define REGULATOR_VOLTAGE 0
#define REGULATOR_CURRENT 1
#define REGULATOR_VOLTAGE_SCALE1 0
#define REGULATOR_VOLTAGE_SCALE2 1
#define REGULATOR_VOLTAGE_SCALE3 2
""",
}

# 预处理指令关键字
_DIRECTIVES = ("include", "define", "undef", "ifdef", "ifndef", "if", "elif",
               "else", "endif", "pragma", "error", "warning", "line")


class PreprocessorError(Exception):
    pass


def _strip_comments_keep_strings(line):
    """去掉 // 注释，但保留字符串内的 //。"""
    out = []
    i = 0
    in_str = False
    while i < len(line):
        c = line[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        else:
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
            elif c == '/' and i + 1 < len(line) and line[i + 1] == '/':
                break
            else:
                out.append(c)
                i += 1
    return ''.join(out)


def _split_args(s):
    """按顶层逗号切分函数宏参数。"""
    args, buf, depth = [], [], 0
    for ch in s:
        if ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf or args:
        args.append(''.join(buf).strip())
    return args


_IDENT = re.compile(r'[A-Za-z_]\w*')


def _expand_line(line, macros, depth=0):
    """展开一行中的所有宏（对象宏 + 函数宏），迭代到稳定。"""
    if depth > 32:
        raise PreprocessorError('宏展开层级过深，可能存在循环定义')
    result = line
    for name, (params, body) in macros.items():
        if params is None:
            # 对象宏
            pattern = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])')
            result = pattern.sub(lambda m: body, result)
        else:
            # 函数宏: NAME(args)
            pattern = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'\s*\(([^()]*)\)')
            prev = None
            while prev != result:
                prev = result
                def repl(m):
                    args = _split_args(m.group(1))
                    rep = body
                    for p, a in zip(params, args):
                        rep = re.sub(r'(?<![A-Za-z0-9_])' + re.escape(p) + r'(?![A-Za-z0-9_])',
                                     lambda mm: a, rep)
                    return rep
                result = pattern.sub(repl, result)
    # 若仍有宏名（嵌套定义），再迭代一轮
    if result != line:
        result = _expand_line(result, macros, depth + 1)
    return result


def _eval_cond(expr, macros):
    """求值 #if / #elif 表达式。"""
    e = re.sub(r'defined\s*\(\s*([A-Za-z_]\w*)\s*\)',
               lambda m: '1' if m.group(1) in macros else '0', expr)
    e = re.sub(r'defined\s+([A-Za-z_]\w*)',
               lambda m: '1' if m.group(1) in macros else '0', e)
    e = _expand_line(e, macros)
    # 未定义标识符按 C 规则视为 0
    e = _IDENT.sub('0', e)
    e = e.replace('&&', ' and ').replace('||', ' or ')
    e = re.sub(r'!(?!=)', ' not ', e)
    try:
        return int(bool(eval(e, {'__builtins__': {}}, {})))
    except Exception:
        return 0


class Preprocessor:
    def __init__(self, include_dirs=None, extra_defines=None):
        self.include_dirs = [Path(d) for d in (include_dirs or [])]
        self.macros = {}          # name -> (params|None, body)
        self.warnings = []
        self._seen_includes = set()
        if extra_defines:
            for item in extra_defines:
                self._define_line(item)

    def _define_line(self, line):
        m = re.match(r'#\s*define\s+([A-Za-z_]\w*)(\(([^)]*)\))?\s*(.*)$', line)
        if not m:
            return
        name = m.group(1)
        params = [p.strip() for p in m.group(3).split(',')] if m.group(2) else None
        body = m.group(4).strip()
        self.macros[name] = (params, body)

    def _resolve_include(self, name, angle, current_file=None):
        # 尖括号: 先内置，再搜索目录
        if not angle and current_file is not None:
            local = Path(current_file).parent / name
            if local.is_file():
                return local.read_text(encoding='utf-8', errors='replace'), str(local)
        if name in BUILTIN_HEADERS:
            return BUILTIN_HEADERS[name], '<builtin:%s>' % name
        for d in self.include_dirs:
            cand = d / name
            if cand.is_file():
                return cand.read_text(encoding='utf-8', errors='replace'), str(cand)
        # 引号形式也尝试内置
        if not angle and name in BUILTIN_HEADERS:
            return BUILTIN_HEADERS[name], '<builtin:%s>' % name
        return None, None

    def process(self, text, current_file=None, depth=0):
        if depth > 32:
            raise PreprocessorError('#include 层级过深')
        lines = text.splitlines()
        out = []
        # 条件编译栈: [parent_active, any_branch_taken, current_active]
        cond = []

        def active():
            return all(f[2] for f in cond)

        for raw in lines:
            line = _strip_comments_keep_strings(raw)
            stripped = line.strip()
            # 指令
            dm = re.match(r'^\s*#\s*([A-Za-z]+)\s*(.*)$', line)
            if dm and dm.group(1) in _DIRECTIVES:
                d, rest = dm.group(1), dm.group(2).strip()
                if d == 'include':
                    if not active():
                        continue
                    im = re.match(r'^[<"]([^>"]+)[>"]', rest)
                    if not im:
                        raise PreprocessorError('#include 语法错误: %s' % raw)
                    name = im.group(1).strip()
                    angle = rest.startswith('<')
                    content, origin = self._resolve_include(name, angle, current_file)
                    if content is None:
                        self.warnings.append('找不到头文件 %s (可在界面上传，或使用 --engine dtc)' % name)
                        continue
                    key = origin
                    if key in self._seen_includes:
                        continue
                    self._seen_includes.add(key)
                    out.append(self.process(content, origin, depth + 1))
                    continue
                elif d == 'define':
                    if active():
                        self._define_line('#define ' + rest)
                    continue
                elif d == 'undef':
                    self.macros.pop(rest.strip(), None)
                    continue
                elif d in ('ifdef', 'ifndef'):
                    name = re.match(r'([A-Za-z_]\w*)', rest)
                    name = name.group(1) if name else ''
                    val = name in self.macros
                    if d == 'ifndef':
                        val = not val
                    cond.append([active(), val, active() and val])
                    continue
                elif d in ('if', 'elif'):
                    if d == 'if':
                        val = _eval_cond(rest, self.macros)
                        cond.append([active(), bool(val), active() and bool(val)])
                    else:
                        if cond:
                            parent, taken, _ = cond[-1]
                            val = (not taken) and _eval_cond(rest, self.macros)
                            cond[-1] = [parent, taken or bool(val), parent and bool(val)]
                    continue
                elif d == 'else':
                    if cond:
                        parent, taken, _ = cond[-1]
                        val = not taken
                        cond[-1] = [parent, True, parent and val]
                    continue
                elif d == 'endif':
                    if cond:
                        cond.pop()
                    continue
                elif d in ('pragma', 'line'):
                    continue
                elif d in ('error', 'warning'):
                    if active():
                        self.warnings.append('%s: %s' % (d, rest))
                    continue
            # dtc 风格的 /include/ "xxx.dtsi"
            im = re.match(r'^\s*/include/\s+"([^"]+)"\s*;?\s*$', line)
            if im:
                if not active():
                    continue
                name = im.group(1)
                content, origin = self._resolve_include(name, False, current_file)
                if content is None:
                    self.warnings.append('找不到 /include/ 文件 %s' % name)
                    continue
                out.append(self.process(content, origin, depth + 1))
                continue
            if active():
                out.append(line)

        expanded = [_expand_line(l, self.macros) for l in out]
        return '\n'.join(expanded)


def preprocess_dts(text, include_dirs=None, extra_defines=None):
    """源码树风格 DTS -> 展开预处理指令后的纯 DTS。返回 (text, warnings)。"""
    pp = Preprocessor(include_dirs=include_dirs, extra_defines=extra_defines)
    result = pp.process(text)
    return result, pp.warnings
