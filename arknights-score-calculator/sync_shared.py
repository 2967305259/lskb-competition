"""Embed the shared calculator in the offline HTML for use without network access."""

from argparse import ArgumentParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'app/static/calculator/score-calculator.js'
OFFLINE = ROOT / 'arknights-score-calculator/集成战略计分器.html'
START = '<!-- BEGIN SHARED SCORE CALCULATOR -->\n'
END = '<!-- END SHARED SCORE CALCULATOR -->'


def synced_html(html, javascript):
    before, separator, remainder = html.partition(START)
    _, closing, after = remainder.partition(END)
    if not separator or not closing:
        raise ValueError('找不到独立计分器中的共享脚本标记')
    if '</script' in javascript.lower():
        raise ValueError('共享脚本包含不能内联的脚本结束标记')
    return before + START + '<script>\n' + javascript.rstrip() + '\n</script>\n' + END + after


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅检查独立版嵌入脚本是否已同步')
    args = parser.parse_args()
    current = OFFLINE.read_text(encoding='utf-8')
    expected = synced_html(current, SOURCE.read_text(encoding='utf-8'))
    if args.check:
        if current != expected:
            parser.exit(1, '共享计分脚本需要重新同步。\n')
    elif current != expected:
        OFFLINE.write_text(expected, encoding='utf-8')


if __name__ == '__main__':
    main()
