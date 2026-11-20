"""
dts_diff.py - DTS 文本差异对比引擎

基于 difflib 做行级对齐，并对修改行做词级（token）差异定位，
返回结构化结果供 CLI（ANSI 标红）与 Web（并排高亮）复用。
纯标准库。
"""

import difflib
import re

# 把一行切成 token：空白、字符串、标识符/数字、其它单字符
_TOKEN_RE = re.compile(r'\s+|"(?:[^"\\]|\\.)*"|[A-Za-z_]\w*|0[xX][0-9a-fA-F]+|\d+|.')


def _tokens(line):
    return _TOKEN_RE.findall(line)


def _word_segments(a, b):
    """对两行做词级 diff，返回各自的片段列表 [{text, changed}]。"""
    ta, tb = _tokens(a), _tokens(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    seg_a, seg_b = [], []

    def add(segs, tokens, i1, i2, changed):
        text = ''.join(tokens[i1:i2])
        if not text:
            return
        if segs and segs[-1]['changed'] == changed:
            segs[-1]['text'] += text
        else:
            segs.append({'text': text, 'changed': changed})

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            add(seg_a, ta, i1, i2, False)
            add(seg_b, tb, j1, j2, False)
        elif tag == 'delete':
            add(seg_a, ta, i1, i2, True)
        elif tag == 'insert':
            add(seg_b, tb, j1, j2, True)
        else:  # replace
            add(seg_a, ta, i1, i2, True)
            add(seg_b, tb, j1, j2, True)
    return seg_a, seg_b


def diff_texts(text_a, text_b, name_a='A', name_b='B', context=3):
    """
    对比两份文本，返回结构化结果。
    rows 中每项:
      {type, a:{num,text,segments}|None, b:{num,text,segments}|None}
      type: equal / replace / delete / insert
    """
    la = text_a.splitlines()
    lb = text_b.splitlines()
    sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)

    rows = []
    stats = {'added': 0, 'removed': 0, 'changed': 0, 'same': 0}

    def plain(line):
        return [{'text': line, 'changed': False}] if line else [{'text': '', 'changed': False}]

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                rows.append({'type': 'equal',
                             'a': {'num': i1 + k + 1, 'text': la[i1 + k], 'segments': plain(la[i1 + k])},
                             'b': {'num': j1 + k + 1, 'text': lb[j1 + k], 'segments': plain(lb[j1 + k])}})
                stats['same'] += 1
        elif tag == 'delete':
            for k in range(i1, i2):
                rows.append({'type': 'delete',
                             'a': {'num': k + 1, 'text': la[k], 'segments': [{'text': la[k], 'changed': True}]},
                             'b': None})
                stats['removed'] += 1
        elif tag == 'insert':
            for k in range(j1, j2):
                rows.append({'type': 'insert',
                             'a': None,
                             'b': {'num': k + 1, 'text': lb[k], 'segments': [{'text': lb[k], 'changed': True}]}})
                stats['added'] += 1
        else:  # replace: 尽量把两行配对做词级高亮
            na, nb = i2 - i1, j2 - j1
            n = max(na, nb)
            for k in range(n):
                a_line = la[i1 + k] if k < na else None
                b_line = lb[j1 + k] if k < nb else None
                if a_line is not None and b_line is not None:
                    sa, sb = _word_segments(a_line, b_line)
                    rows.append({'type': 'replace',
                                 'a': {'num': i1 + k + 1, 'text': a_line, 'segments': sa},
                                 'b': {'num': j1 + k + 1, 'text': b_line, 'segments': sb}})
                    stats['changed'] += 1
                elif a_line is not None:
                    rows.append({'type': 'delete',
                                 'a': {'num': i1 + k + 1, 'text': a_line,
                                       'segments': [{'text': a_line, 'changed': True}]},
                                 'b': None})
                    stats['removed'] += 1
                else:
                    rows.append({'type': 'insert',
                                 'a': None,
                                 'b': {'num': j1 + k + 1, 'text': b_line,
                                       'segments': [{'text': b_line, 'changed': True}]}})
                    stats['added'] += 1

    stats['lines_a'] = len(la)
    stats['lines_b'] = len(lb)
    stats['different'] = stats['added'] + stats['removed'] + stats['changed']
    return {
        'ok': True,
        'name_a': name_a,
        'name_b': name_b,
        'rows': rows,
        'stats': stats,
        'identical': (text_a == text_b),
    }


# ---------------------------------------------------------------------------
# CLI 输出（ANSI 标红）
# ---------------------------------------------------------------------------

_RED = '\033[31m'
_RED_BG = '\033[41m'
_GREEN = '\033[32m'
_DIM = '\033[2m'
_RESET = '\033[0m'


def _color_segments(segments, use_color):
    """把 changed 片段用红色背景标出。"""
    out = []
    for seg in segments:
        txt = seg['text']
        if seg['changed'] and use_color:
            out.append(_RED_BG + txt + _RESET)
        else:
            out.append(txt)
    return ''.join(out)


def diff_text_report(result, use_color=True):
    """渲染为 CLI 文本报告；差异行整行标红，词级差异用红底。"""
    L = []
    st = result['stats']
    L.append('=' * 64)
    L.append('DTS 差异对比: %s  vs  %s' % (result['name_a'], result['name_b']))
    L.append('=' * 64)
    L.append('A: %d 行   B: %d 行   新增 %d / 删除 %d / 修改 %d / 相同 %d'
             % (st['lines_a'], st['lines_b'], st['added'], st['removed'], st['changed'], st['same']))
    L.append('')
    for row in result['rows']:
        t = row['type']
        a, b = row['a'], row['b']
        if t == 'equal':
            L.append('  %4d │ %s' % (a['num'], a['text']) if a else '        │ %s' % b['text'])
        elif t == 'delete':
            marked = _color_segments(a['segments'], use_color)
            L.append((_RED if use_color else '') + '- %4d │ %s' % (a['num'], marked) + (_RESET if use_color else ''))
        elif t == 'insert':
            marked = _color_segments(b['segments'], use_color)
            L.append((_RED if use_color else '') + '+       │ %s' % marked + (_RESET if use_color else ''))
        else:  # replace
            if a:
                marked = _color_segments(a['segments'], use_color)
                L.append((_RED if use_color else '') + '- %4d │ %s' % (a['num'], marked) + (_RESET if use_color else ''))
            if b:
                marked = _color_segments(b['segments'], use_color)
                L.append((_RED if use_color else '') + '+ %4d │ %s' % (b['num'], marked) + (_RESET if use_color else ''))
    if result['identical']:
        L.append('')
        L.append('两个文件内容完全一致。')
    return '\n'.join(L)
