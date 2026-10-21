import json
from pathlib import Path
import socket
import time
import unittest
from unittest.mock import Mock, patch

from news_monitor import article_content as content


PUBLISHER = "https://publisher.example.org/news/event"
BODY = "中国工商银行金边分行与当地企业代表参加交流会议，共同讨论跨境金融服务与中柬经贸合作的发展机会。"
UNRELATED = "本次会议讨论当地农业生产、出口物流和职业培训，参会代表就下一年度的工作计划交换意见并提出建议。"


def article(body=BODY):
    return f"<html><title>Local event</title><article><h1>Meeting in Cambodia</h1><p>{body}</p></article></html>".encode()


class FakeResponse:
    def __init__(self, status=200, body=b"", **headers):
        self.status, self.body, self.offset = status, body, 0
        self.headers = {"Content-Type": "text/html; charset=utf-8", **headers}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount):
        data = self.body[self.offset:self.offset + amount]
        self.offset += len(data)
        return data


class ArticleBodyTests(unittest.TestCase):
    def test_body_only_chinese_branch_is_confirmed(self):
        result = content.inspect_article(PUBLISHER, fetcher=lambda _: article())
        self.assertTrue(result.verified)
        self.assertTrue(result.matched)
        self.assertIn("中国工商银行金边分行", result.body_excerpt)
        self.assertEqual(result.resolved_url, PUBLISHER)

    def test_supported_main_containers(self):
        for attributes in ('id="content"', 'class="article-content"', 'id="rich_media_content"', 'itemprop="articleBody"'):
            with self.subTest(attributes=attributes):
                result = content.extract_article_html(f"<div {attributes}><p>{BODY}</p></div>")
                self.assertTrue(result.matched)

    def test_english_full_alias_and_same_sentence_bank_city_context(self):
        for text in (
            "ICBC Phnom Penh Branch joined local enterprises in discussing cross-border financial services and trade cooperation.",
            "Industrial and Commercial Bank of China Limited, Phnom Penh Branch joined the event with several representatives.",
            "At a meeting in Phnom Penh, the Industrial and Commercial Bank of China discussed financial services with local businesses.",
        ):
            with self.subTest(text=text):
                self.assertTrue(content.extract_article_html(article(text)).matched)

    def test_header_sidebar_footer_and_related_do_not_establish_relevance(self):
        for extra in (
            f"<header><p>{BODY}</p></header>",
            f"<aside><article><p>{BODY}</p></article></aside>",
            f"<footer><div id='content'><p>{BODY}</p></div></footer>",
            f"<div class='related-articles'><article><p>{BODY}</p></article></div>",
            f"<div aria-hidden='true'><article><p>{BODY}</p></article></div>",
            f"<nav><div class='article-content'><p>{BODY}</p></div></nav>",
        ):
            with self.subTest(extra=extra[:35]):
                result = content.extract_article_html(f"<main><article><p>{UNRELATED}</p></article>{extra}</main>")
                self.assertTrue(result.verified)
                self.assertFalse(result.matched)

    def test_title_or_heading_only_mention_does_not_establish_body_match(self):
        result = content.extract_article_html(f"<title>{BODY}</title><article><h1>{BODY}</h1><p>{UNRELATED}</p></article>")
        self.assertTrue(result.verified)
        self.assertFalse(result.matched)

    def test_related_heading_stops_following_recommendations(self):
        result = content.extract_article_html(f"<article><p>{UNRELATED}</p><h2>相关阅读</h2><p>{BODY}</p></article>")
        self.assertTrue(result.verified)
        self.assertFalse(result.matched)

    def test_script_style_and_metadata_are_not_article_prose(self):
        result = content.extract_article_html(f"<article><script>{BODY}</script><style>{BODY}</style><div class='author-info'>{BODY}</div><p>{UNRELATED}</p></article>")
        self.assertTrue(result.verified)
        self.assertFalse(result.matched)

    def test_separate_sentences_or_paragraphs_do_not_join_bank_and_city(self):
        for body in (
            "ICBC announced its global annual financial results. Phnom Penh hosted an unrelated agricultural training event for local enterprises.",
            "<p>ICBC announced its global annual financial results with healthy business growth.</p><p>Phnom Penh hosted an unrelated agricultural training event.</p>",
        ):
            with self.subTest(body=body):
                result = content.extract_article_html(f"<article>{body}</article>")
                self.assertTrue(result.verified)
                self.assertFalse(result.matched)

    def test_canadian_insurance_and_cambodia_alone_are_not_branch_context(self):
        for text in (
            "The Insurance Corporation of British Columbia (ICBC) considered a car insurance claim from a driver returning from Phnom Penh.",
            "ICBC discussed investment in Cambodia and its global financial strategy with international business leaders.",
        ):
            with self.subTest(text=text):
                self.assertFalse(content.extract_article_html(article(text)).matched)

    def test_jsonld_article_body_is_supported(self):
        data = {"@context": "https://schema.org", "@type": "NewsArticle", "url": PUBLISHER, "articleBody": BODY}
        result = content.extract_article_html('<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>", resolved_url=PUBLISHER)
        self.assertTrue(result.verified)
        self.assertTrue(result.matched)

    def test_jsonld_related_article_other_url_and_sidebar_are_excluded(self):
        related = {"@type": "NewsArticle", "url": "https://publisher.example.org/related", "articleBody": BODY}
        html = f"<article><p>{UNRELATED}</p></article><script type='application/ld+json'>{json.dumps(related)}</script>"
        self.assertFalse(content.extract_article_html(html, resolved_url=PUBLISHER).matched)
        sidebar = f"<article><p>{UNRELATED}</p></article><aside><script type='application/ld+json'>{json.dumps(related)}</script></aside>"
        self.assertFalse(content.extract_article_html(sidebar, resolved_url=PUBLISHER).matched)

    def test_multiple_anonymous_jsonld_articles_are_not_assumed_main(self):
        graph = {"@graph": [{"@type": "NewsArticle", "articleBody": UNRELATED}, {"@type": "NewsArticle", "articleBody": BODY}]}
        result = content.extract_article_html(f"<script type='application/ld+json'>{json.dumps(graph)}</script>")
        self.assertFalse(result.verified)
        self.assertFalse(result.matched)

    def test_jsonld_identity_preserves_article_query_but_ignores_fragment(self):
        schema = {"@type": "NewsArticle", "@id": "https://publisher.example.org/story?id=2#article", "articleBody": BODY}
        html = f"<article><p>{UNRELATED}</p></article><script type='application/ld+json'>{json.dumps(schema)}</script>"
        self.assertFalse(content.extract_article_html(html, resolved_url="https://publisher.example.org/story?id=1").matched)
        self.assertTrue(content.extract_article_html(html, resolved_url="https://publisher.example.org/story?id=2").matched)

    def test_no_main_container_or_unreadable_body_is_unverified(self):
        for html in (f"<body><p>{BODY}</p></body>", "<article>中国工商银行金边分行</article>", "<html><body>Sign in</body></html>"):
            with self.subTest(html=html[:30]):
                result = content.extract_article_html(html)
                self.assertFalse(result.verified)
                self.assertFalse(result.matched)

    def test_search_landing_pages_never_confirm_body_evidence(self):
        for host in ("news.google.com", "www.baidu.com", "www.bing.com"):
            with self.subTest(host=host):
                result = content.inspect_article(f"https://{host}/news", fetcher=lambda _: article())
                self.assertFalse(result.verified)
                self.assertFalse(result.matched)
                self.assertIn("Search landing", result.error)

    def test_search_wrapper_resolving_supported_publisher_can_confirm(self):
        result = content.inspect_article("https://news.google.com/articles/opaque", fetcher=lambda _: (article(), PUBLISHER))
        self.assertTrue(result.matched)
        self.assertEqual(result.resolved_url, PUBLISHER)

    def test_security_challenge_html_is_unverified(self):
        result = content.extract_article_html(f"<title>百度安全验证</title><article><p>{BODY}</p></article>")
        self.assertFalse(result.verified)
        self.assertFalse(result.matched)

    def test_oversized_and_complex_documents_are_bounded(self):
        self.assertFalse(content.extract_article_html(b"x" * (content.MAX_ARTICLE_BYTES + 1)).verified)
        result = content.extract_article_html("<div>" * 205 + BODY + "</div>" * 205)
        self.assertFalse(result.verified)
        self.assertIn("complex", result.error)

    def test_explicit_chinese_encoding_and_excerpt_limit(self):
        result = content.extract_article_html(article().decode().encode("gb18030"), content_type="text/html; charset=gb18030")
        self.assertTrue(result.matched)
        long = content.extract_article_html(article("地方新闻。" * 200 + BODY + "延伸说明。" * 200))
        self.assertTrue(long.matched)
        self.assertLessEqual(len(long.body_excerpt), 650)


class ArticleNetworkPolicyTests(unittest.TestCase):
    def test_denied_hosts_credentials_auth_paths_and_private_ips_never_call_fetcher(self):
        for url in (
            "https://baijiahao.baidu.com/s?id=123", "https://sub.baijiahao.baidu.com/x",
            "https://wappass.baidu.com/x", "https://passport.baidu.com/x",
            "https://login.example.org/x", "https://publisher.example.org/captcha/check",
            "http://publisher.example.org/news", "https://user:secret@publisher.example.org/news",
            "https://publisher.example.org:8443/news", "https://127.0.0.1/news",
            "https://127.1/news", "https://[::1]/news", "https://169.254.169.254/latest",
            "https://localhost/news", "https://host.internal/news",
        ):
            with self.subTest(url=url):
                fetcher = Mock(return_value=article())
                result = content.inspect_article(url, fetcher=fetcher)
                self.assertFalse(result.verified)
                self.assertFalse(result.matched)
                fetcher.assert_not_called()
                self.assertNotIn("secret", result.error)

    def test_injected_final_denied_destination_cannot_confirm_body(self):
        result = content.inspect_article("https://news.google.com/articles/opaque", fetcher=lambda _: (article(), "https://baijiahao.baidu.com/s?id=123"))
        self.assertFalse(result.verified)
        self.assertFalse(result.matched)

    def test_all_dns_addresses_must_be_public(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))
        with patch.object(content.socket, "getaddrinfo", return_value=[public, private]):
            with self.assertRaises(content.ArticleContentError):
                content._resolve_public_addresses("publisher.example.org", time.monotonic() + 1)
        with patch.object(content.socket, "getaddrinfo", return_value=[public]):
            self.assertEqual(content._resolve_public_addresses("publisher.example.org", time.monotonic() + 1), ["8.8.8.8"])

    def test_pinned_connection_preserves_tls_hostname_without_second_dns_lookup(self):
        raw, context, wrapped = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = wrapped
        with patch.object(content.ssl, "create_default_context", return_value=context), patch.object(content.socket, "socket", return_value=raw), patch.object(content.socket, "getaddrinfo") as dns:
            connection = content._PinnedHTTPSConnection("publisher.example.org", "8.8.8.8", 3)
            connection.connect()
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="publisher.example.org")
        dns.assert_not_called()
        self.assertIs(connection.sock, wrapped)

    def fake_connections(self, responses):
        calls = []

        class Connection:
            sock = None

            def __init__(self, host, address, timeout):
                calls.append((host, address, timeout))

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return responses.pop(0)

            def close(self):
                pass

        return Connection, calls

    def test_google_redirect_to_denied_publisher_is_stopped_before_connect(self):
        connection, calls = self.fake_connections([FakeResponse(302, Location="https://baijiahao.baidu.com/s?id=123")])
        with patch.object(content, "_PinnedHTTPSConnection", connection), patch.object(content, "_resolve_public_addresses", return_value=["8.8.8.8"]) as dns:
            result = content.inspect_article("https://news.google.com/articles/opaque")
        self.assertFalse(result.verified)
        self.assertEqual([call[0] for call in calls], ["news.google.com"])
        self.assertEqual(dns.call_count, 1)

    def test_redirect_to_private_ip_or_http_is_stopped(self):
        for destination in ("https://127.0.0.1/news", "http://publisher.example.org/news"):
            with self.subTest(destination=destination):
                connection, calls = self.fake_connections([FakeResponse(302, Location=destination)])
                with patch.object(content, "_PinnedHTTPSConnection", connection), patch.object(content, "_resolve_public_addresses", return_value=["8.8.8.8"]):
                    result = content.inspect_article(PUBLISHER)
                self.assertFalse(result.verified)
                self.assertEqual(len(calls), 1)

    def test_no_more_than_three_redirects_and_no_retry(self):
        connection, calls = self.fake_connections([FakeResponse(302, Location="/next")] * 4)
        with patch.object(content, "_PinnedHTTPSConnection", connection), patch.object(content, "_resolve_public_addresses", return_value=["8.8.8.8"]):
            result = content.inspect_article(PUBLISHER)
        self.assertFalse(result.verified)
        self.assertEqual(len(calls), 4)
        self.assertIn("redirect limit", result.error)

    def test_supported_redirect_returns_direct_publisher_url(self):
        connection, calls = self.fake_connections([FakeResponse(302, Location=PUBLISHER), FakeResponse(body=article())])
        with patch.object(content, "_PinnedHTTPSConnection", connection), patch.object(content, "_resolve_public_addresses", return_value=["8.8.8.8"]):
            result = content.inspect_article("https://news.google.com/articles/opaque")
        self.assertTrue(result.matched)
        self.assertEqual(result.resolved_url, PUBLISHER)
        self.assertEqual([call[0] for call in calls], ["news.google.com", "publisher.example.org"])

    def test_response_type_size_and_status_fail_without_retries(self):
        for response in (
            FakeResponse(503), FakeResponse(body=b"%PDF", **{"Content-Type": "application/pdf"}),
            FakeResponse(**{"Content-Length": str(content.MAX_ARTICLE_BYTES + 1)}),
            FakeResponse(body=b"x" * (content.MAX_ARTICLE_BYTES + 1)),
        ):
            with self.subTest(status=response.status, headers=response.headers):
                connection, calls = self.fake_connections([response])
                with patch.object(content, "_PinnedHTTPSConnection", connection), patch.object(content, "_resolve_public_addresses", return_value=["8.8.8.8"]):
                    result = content.inspect_article(PUBLISHER)
                self.assertFalse(result.verified)
                self.assertEqual(len(calls), 1)

    def test_transport_exception_details_are_not_exposed(self):
        def fail(url):
            raise OSError("private transport URL with access_token=secret and recipient=123")

        result = content.inspect_article(PUBLISHER, fetcher=fail)
        self.assertFalse(result.verified)
        self.assertNotIn("secret", result.error)
        self.assertNotIn("recipient", result.error)


if __name__ == "__main__":
    unittest.main()
