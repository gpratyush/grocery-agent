from pathlib import Path

from grocery_agent.sourcing import extract_recipe, looks_like_recipe_url, site_query, source

HTML = (Path(__file__).parent / "fixtures" / "curry.html").read_text()


class FakeSearch:
    def __init__(self, urls):
        self.urls, self.queries = urls, []

    def search(self, query, max_results):
        self.queries.append(query)
        return self.urls


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.fetched = pages, []

    def get(self, url):
        self.fetched.append(url)
        return self.pages.get(url)


def test_extract_recipe_from_schema_org():
    r = extract_recipe(HTML, "https://www.example.com/red-curry", cuisine_hint="thai")
    assert r.title == "Easy Red Curry with Tofu"
    assert r.cuisine == "thai" and r.servings == 4 and r.total_time == 35 and r.site == "example.com"
    assert r.nutrition == {"calories": 520, "protein_g": 22, "carbs_g": 60, "fat_g": 24}
    assert len(r.ingredients) == 6 and "dinner" in r.tags


def test_extract_rejects_non_recipe_page():
    assert extract_recipe("<html><body>hello</body></html>", "https://x.com/a") is None


def test_source_skips_known_and_listing_urls_and_caps_pages():
    urls = ["https://ex.com/red-curry", "https://ex.com/tag/thai/", "https://ex.com/known", "https://ex.com/other",
            "https://ex.com/third"]
    search = FakeSearch(urls)
    fetcher = FakeFetcher({"https://ex.com/red-curry": HTML, "https://ex.com/other": "<html></html>"})
    found = source(["red curry tofu"], ["ex.com"], "thai", search=search, fetcher=fetcher, max_pages=2,
                   known_url=lambda u: u.endswith("/known"))
    assert [r.url for r in found] == ["https://ex.com/red-curry"]
    assert fetcher.fetched == ["https://ex.com/red-curry", "https://ex.com/other"]
    assert search.queries == ["red curry tofu recipe site:ex.com"]


def test_helpers():
    assert site_query("laab", ["a.com", "b.com"]) == "laab recipe site:a.com OR site:b.com"
    assert not looks_like_recipe_url("https://a.com/")
    assert not looks_like_recipe_url("https://a.com/category/dinner")
