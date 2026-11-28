"""Embed the shared calculator in the offline HTML for use without network access."""

from argparse import ArgumentParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'app/static/calculator/score-calculator.js'
CONFIGURATION = ROOT / 'app/static/calculator/default-config.json'
OFFLINE = ROOT / 'arknights-score-calculator/集成战略计分器.html'
START = '<!-- BEGIN SHARED SCORE CALCULATOR -->\n'
END = '<!-- END SHARED SCORE CALCULATOR -->'
CONFIG_START = '// BEGIN BUILTIN SCORE CONFIGURATION\n'
CONFIG_END = '// END BUILTIN SCORE CONFIGURATION'


def replace_block(html, start, end, contents):
    before, separator, remainder = html.partition(start)
    _, closing, after = remainder.partition(end)
    if not separator or not closing:
        raise ValueError(f'找不到独立计分器中的标记 {start.strip()}')
    return before + start + contents + end + after


def synced_html(html, javascript, configuration):
    if '</script' in javascript.lower():
        raise ValueError('共享脚本包含不能内联的脚本结束标记')
    html = replace_block(html, START, END, '<script>\n' + javascript.rstrip() + '\n</script>\n')
    configuration = configuration.rstrip().replace('<', '\\u003c')
    return replace_block(html, CONFIG_START, CONFIG_END, f'const BUILTIN_CONFIG = {configuration};\n')


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅检查独立版嵌入脚本是否已同步')
    args = parser.parse_args()
    current = OFFLINE.read_text(encoding='utf-8')
    expected = synced_html(current, SOURCE.read_text(encoding='utf-8'), CONFIGURATION.read_text(encoding='utf-8'))
    if args.check:
        if current != expected:
            parser.exit(1, '共享计分脚本需要重新同步。\n')
    elif current != expected:
        OFFLINE.write_text(expected, encoding='utf-8')


if __name__ == '__main__':
    main()
