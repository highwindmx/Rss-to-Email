import feedparser, os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

raw = os.environ.get("RSS_URLS") or os.environ.get("RSS_URL", "")
urls = [u.strip() for u in raw.split(",") if u.strip()]
print(f"解析到 {len(urls)} 个 RSS 源\n")
for url in urls:
    print("=" * 60)
    print("源:", url)
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            print("  [警告] 解析异常:", repr(feed.bozo_exception))
        ch = feed.feed or {}
        print("  频道标题:", ch.get("title", "(无)"))
        print("  条目数:", len(feed.entries))
        for i, e in enumerate(feed.entries[:5], 1):
            print(f"   {i}. {e.get('title', '(无标题)')}")
            print(f"      {e.get('link', '')}")
    except Exception as ex:
        print("  [异常]", ex)
    print()
