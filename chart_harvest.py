"""把 x_search 找到的"带图帖"收进图表池。

用法：
    python chart_harvest.py 2119086571643122070 2123956787514671237 ...
    python chart_harvest.py --list          # 看池子里有什么
    python chart_harvest.py --clear         # 清空

为什么不自动扫账号：X 的 timeline 接口在本机一直 429；发现靠 agent 的 x_search。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg2x import charts  # noqa: E402
from tg2x.config import load_dotenv  # noqa: E402


def main(argv: list[str]) -> int:
    load_dotenv()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        items = charts.load()
        print(f"图表池 {len(items)} 条")
        for it in items:
            print(f"  {it.get('id')}  @{it.get('author') or '?':16s} "
                  f"{(it.get('created') or '')[:10]}  {(it.get('text') or '')[:70]}")
        return 0
    if argv[0] == "--clear":
        charts.save([])
        print("已清空")
        return 0
    stat = charts.harvest(argv)
    print(f"新增 {stat['added']} 条，池子共 {stat['total']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
