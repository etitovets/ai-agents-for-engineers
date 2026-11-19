"""Representative RSS documents used by the offline tests."""

VALID_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Заголовок новости на русском</title>
    <link>https://ria.ru/2024/01/15/article1.html</link>
    <guid>ria-1</guid>
    <pubDate>Mon, 15 Jan 2024 12:00:00 +0300</pubDate>
    <description><![CDATA[<p>Описание новости с <b>HTML</b> разметкой</p><script>alert(1)</script>]]></description>
  </item>
  <item>
    <title>Вторая новость</title>
    <link>https://tass.ru/2024/01/15/article2.html?id=42</link>
    <pubDate>Tue, 16 Jan 2024 08:30:00 +0000</pubDate>
    <description>Простое описание без HTML</description>
  </item>
  <item>
    <title>Дубликат второй новости</title>
    <link>https://tass.ru/2024/01/15/article2.html?id=42</link>
    <description>Та же ссылка</description>
  </item>
</channel>
</rss>"""

RSS_MISSING_FIELDS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Нет даты публикации</title>
    <link>https://example.ru/no-date.html</link>
    <description>Есть описание но нет даты</description>
  </item>
  <item>
    <link>https://example.ru/no-title.html</link>
    <description>Есть описание но нет заголовка</description>
  </item>
  <item>
    <title>Нет ссылки</title>
    <description>Нет ссылки на статью</description>
  </item>
</channel>
</rss>"""

RSS_MALFORMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <item>
    <title>Хорошая новость</title>
    <link>https://example.ru/good.html</link>
    <description>Текст</description>
  </item>
  <item><title>Обрыв
"""

EMPTY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty Feed</title></channel></rss>"""


def single_item_rss(title: str, link: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>{title}</title><link>{link}</link>
<pubDate>Mon, 15 Jan 2024 12:00:00 +0000</pubDate>
<description>{title} — текст новости</description></item>
</channel></rss>"""
