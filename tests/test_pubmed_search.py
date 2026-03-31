import argparse
import builtins
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from requests import HTTPError


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "pubmed_search.py"
SKILL_PATH = ROOT / "SKILL.md"


def load_module():
    spec = importlib.util.spec_from_file_location("pubmed_search_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_args(**overrides):
    defaults = {
        "max_results": None,
        "years": None,
        "article_types": None,
        "species": None,
        "sort_by": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Gut microbiota and Alzheimer's disease in mice</ArticleTitle>
        <Abstract>
          <AbstractText>This review examines microbiota-brain interactions in murine Alzheimer's disease models.</AbstractText>
        </Abstract>
        <Journal>
          <ISOAbbreviation>Neurobiol Dis</ISOAbbreviation>
          <JournalIssue>
            <PubDate>
              <Year>2024</Year>
            </PubDate>
          </JournalIssue>
        </Journal>
      </Article>
      <PublicationTypeList>
        <PublicationType>Review</PublicationType>
      </PublicationTypeList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/example-doi</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

PUBMED_XML_WITH_UNICODE = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Gut microbiota and β-amyloid signaling</ArticleTitle>
        <Abstract>
          <AbstractText>β-catenin and gut microbiota interactions are reviewed.</AbstractText>
        </Abstract>
        <Journal>
          <ISOAbbreviation>Neurobiol Dis</ISOAbbreviation>
          <JournalIssue>
            <PubDate>
              <Year>2024</Year>
            </PubDate>
          </JournalIssue>
        </Journal>
      </Article>
      <PublicationTypeList>
        <PublicationType>Review</PublicationType>
      </PublicationTypeList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/example-doi</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

PUBMED_XML_MISSING_FIELDS = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>87654321</PMID>
      <Article>
        <ArticleTitle>Aspirin effects on gut microbiota</ArticleTitle>
        <Journal>
          <Title>Example Journal</Title>
        </Journal>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList />
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def make_response(content, *, content_type="application/pdf", url="https://example.org/paper.pdf", json_payload=None, text=None):
    response = mock.Mock()
    response.headers = {"Content-Type": content_type}
    response.content = content if isinstance(content, bytes) else content.encode("utf-8")
    response.text = text if text is not None else (content if isinstance(content, str) else content.decode("utf-8", errors="ignore"))
    response.url = url
    response.raise_for_status.return_value = None
    if json_payload is not None:
        response.json.return_value = json_payload
    return response


class PubMedSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_frontmatter_required_fields_preserved(self):
        content = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("name: pubmed_paper_finder", content)
        self.assertIn("description:", content)
        self.assertIn("metadata:", content)
        self.assertIn("user-invocable: true", content)

    def test_cli_has_no_output_argument(self):
        parser = self.module.build_argument_parser()
        help_text = parser.format_help()
        self.assertNotIn("--output", help_text)
        self.assertIn("--query-stdin", help_text)
        self.assertIn("--self-check", help_text)
        self.assertIn("--download-request-stdin", help_text)

    def test_recent_review_request_defaults_to_top10(self):
        params = self.module.parse_request(
            "Find recent review papers on gut microbiota and Alzheimer's disease in mice",
            build_args(),
        )
        self.assertEqual(params.max_results, 10)
        self.assertEqual(params.article_type_filter_mode, "boost")
        self.assertIn("review", params.article_types)
        self.assertIn("mouse", params.species)

    def test_known_and_unknown_concepts_are_both_preserved(self):
        params = self.module.parse_request(
            "Find review papers on gut microbiota and aspirin",
            build_args(),
        )
        built_query = self.module.build_pubmed_query(params)
        self.assertEqual(len(params.keyword_groups), 2)
        self.assertIn("gut microbiota", params.keyword_groups[0])
        self.assertEqual(params.keyword_groups[1], ["aspirin"])
        self.assertIn('"aspirin"[Title/Abstract]', built_query)

    def test_known_and_free_text_multiconcept_query_is_not_collapsed(self):
        params = self.module.parse_request(
            "Find papers on gut microbiota and beta amyloid signaling",
            build_args(),
        )
        built_query = self.module.build_pubmed_query(params)
        self.assertEqual(len(params.keyword_groups), 2)
        self.assertIn("gut microbiota", params.keyword_groups[0])
        self.assertEqual(params.keyword_groups[1], ["beta amyloid signaling"])
        self.assertIn('"beta amyloid signaling"[Title/Abstract]', built_query)

    def test_excluding_case_reports_becomes_negative_type(self):
        params = self.module.parse_request(
            "Find meta-analysis papers on colorectal cancer excluding case reports",
            build_args(),
        )
        self.assertIn("meta-analysis", params.article_types)
        self.assertIn("case report", params.excluded_article_types)
        self.assertNotIn("case report", params.article_types)

    def test_path_text_removed_from_query(self):
        request = "Find meta-analysis papers on colorectal cancer excluding case reports and save to D:\\papers\\crc"
        params = self.module.parse_request(request, build_args())
        built_query = self.module.build_pubmed_query(params)
        self.assertNotIn("D:\\papers\\crc", params.topic)
        self.assertNotIn("D:\\papers\\crc", " ".join(params.exclude_terms))
        self.assertNotIn("D:\\papers\\crc", built_query)

    def test_stdout_json_schema(self):
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find recent review papers on gut microbiota and Alzheimer's disease in mice"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["query"], "Find recent review papers on gut microbiota and Alzheimer's disease in mice")
        self.assertIn("parsed_search_parameters", payload)
        self.assertIn("pubmed_query", payload)
        self.assertIn("retrieved_count", payload)
        self.assertIn("selected_count", payload)
        self.assertIn("download_context", payload)
        self.assertIn("results", payload)
        self.assertNotIn("output_folder", json.dumps(payload))
        self.assertEqual(stderr.getvalue(), "")

    def test_search_output_includes_numbered_results_and_download_hints(self):
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find recent review papers on gut microbiota and Alzheimer's disease in mice"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["results"][0]["result_index"], 1)
        self.assertEqual(payload["results"][0]["pmcid"], "")
        self.assertIn("download_hints", payload["results"][0])
        self.assertIn("folder_name", payload["download_context"])
        self.assertEqual(stderr.getvalue(), "")

    def test_query_can_be_read_from_stdin(self):
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML), \
             mock.patch("sys.stdin", io.StringIO("Find review papers on gut microbiota")):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query-stdin"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["query"], "Find review papers on gut microbiota")
        self.assertEqual(stderr.getvalue(), "")

    def test_utf8_stdin_query_preserves_non_ascii_text(self):
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML), \
             mock.patch("sys.stdin", io.StringIO("Find papers on β-amyloid and 肠道菌群")):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query-stdin"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["query"], "Find papers on β-amyloid and 肠道菌群")
        self.assertEqual(stderr.getvalue(), "")

    def test_stdout_payload_is_safe_for_gbk_stdout(self):
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML_WITH_UNICODE):
            raw_stdout = io.BytesIO()
            gbk_stdout = io.TextIOWrapper(raw_stdout, encoding="gbk", errors="strict")
            stderr = io.StringIO()
            with redirect_stdout(gbk_stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find review papers on gut microbiota"])
            gbk_stdout.flush()
        self.assertEqual(exit_code, 0)
        output_text = raw_stdout.getvalue().decode("gbk")
        self.assertIn("\\u03b2", output_text)
        self.assertEqual(stderr.getvalue(), "")

    def test_no_file_write_in_main_flow(self):
        real_open = builtins.open

        def fail_on_write(*args, **kwargs):
            mode = kwargs.get("mode")
            if mode is None and len(args) > 1:
                mode = args[1]
            mode = mode or "r"
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("unexpected write attempt")
            return real_open(*args, **kwargs)

        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", return_value={"esearchresult": {"count": "1", "idlist": ["12345678"]}}), \
             mock.patch.object(self.module, "request_text", return_value=PUBMED_XML), \
             mock.patch("builtins.open", side_effect=fail_on_write):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find review papers on gut microbiota"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_error_path_writes_nothing(self):
        real_open = builtins.open

        def fail_on_write(*args, **kwargs):
            mode = kwargs.get("mode")
            if mode is None and len(args) > 1:
                mode = args[1]
            mode = mode or "r"
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("unexpected write attempt")
            return real_open(*args, **kwargs)

        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", side_effect=self.module.RequestsRequestException("boom")), \
             mock.patch("builtins.open", side_effect=fail_on_write):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find review papers on gut microbiota"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("PubMed request failed", stderr.getvalue())

    def test_blocked_socket_error_includes_actionable_hint(self):
        blocked_error = self.module.RequestsRequestException(
            "HTTPSConnectionPool(host='eutils.ncbi.nlm.nih.gov', port=443): "
            "Failed to establish a new connection: [WinError 10013]"
        )
        with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
             mock.patch.object(self.module, "request_json", side_effect=blocked_error):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--query", "Find review papers on gut microbiota"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("eutils.ncbi.nlm.nih.gov:443", stderr.getvalue())
        self.assertIn("blocked by firewall, proxy, VPN, or endpoint policy", stderr.getvalue())

    def test_self_check_returns_json_payload(self):
        with mock.patch.object(self.module, "probe_pubmed_connectivity", return_value=(True, "Reached PubMed ESearch over HTTPS.")):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--self-check"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["dependency_check"]["ok"])
        self.assertTrue(payload["pubmed_connectivity"]["ok"])
        self.assertIn("python_version", payload["runtime"])
        self.assertEqual(stderr.getvalue(), "")

    def test_self_check_reports_connectivity_failure(self):
        with mock.patch.object(self.module, "probe_pubmed_connectivity", return_value=(False, "blocked")):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = self.module.main(["--self-check"])
        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["pubmed_connectivity"]["ok"])
        self.assertEqual(payload["pubmed_connectivity"]["detail"], "blocked")
        self.assertEqual(stderr.getvalue(), "")

    def test_http_request_retries_after_429_then_succeeds(self):
        ok_response = mock.Mock()
        ok_response.raise_for_status.return_value = None
        ok_response.json.return_value = {"esearchresult": {"count": "1", "idlist": ["12345678"]}}
        rate_limited_response = mock.Mock(status_code=429)
        rate_limited_error = HTTPError("429")
        rate_limited_error.response = rate_limited_response
        with mock.patch.object(self.module.requests, "get", side_effect=[rate_limited_error, ok_response]), \
             mock.patch.object(self.module.time, "sleep") as sleep_mock:
            payload = self.module.request_json(self.module.ESEARCH_URL, {"term": "gut microbiota"})
        self.assertEqual(payload["esearchresult"]["count"], "1")
        sleep_mock.assert_called_once()

    def test_http_request_retries_after_500_then_succeeds(self):
        ok_response = mock.Mock()
        ok_response.raise_for_status.return_value = None
        ok_response.text = PUBMED_XML
        server_error_response = mock.Mock(status_code=500)
        server_error = HTTPError("500")
        server_error.response = server_error_response
        with mock.patch.object(self.module.requests, "get", side_effect=[server_error, ok_response]), \
             mock.patch.object(self.module.time, "sleep") as sleep_mock:
            payload = self.module.request_text(self.module.EFETCH_URL, {"id": "12345678"})
        self.assertIn("PubmedArticleSet", payload)
        sleep_mock.assert_called_once()

    def test_http_request_gives_up_after_repeated_failures(self):
        timeout_error = self.module.RequestsRequestException("boom")
        with mock.patch.object(self.module.requests, "get", side_effect=timeout_error), \
             mock.patch.object(self.module.time, "sleep") as sleep_mock:
            with self.assertRaises(self.module.RequestsRequestException):
                self.module.request_json(self.module.ESEARCH_URL, {"term": "gut microbiota"})
        self.assertEqual(sleep_mock.call_count, self.module.HTTP_RETRY_ATTEMPTS - 1)

    def test_parse_pubmed_xml_with_missing_fields(self):
        papers = self.module.parse_pubmed_xml(PUBMED_XML_MISSING_FIELDS)
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.doi, "")
        self.assertEqual(paper.abstract, "")
        self.assertIsNone(paper.year)
        self.assertEqual(paper.journal, "Example Journal")

    def test_parse_pubmed_xml_error_is_reported(self):
        with self.assertRaises(self.module.PubMedSearchError):
            self.module.parse_pubmed_xml("<not xml")

    def test_invalid_species_override_returns_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = self.module.main(["--query", "Find review papers on gut microbiota", "--species", "dog"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Unsupported species override", stderr.getvalue())

    def test_negative_max_results_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.module.build_argument_parser().parse_args(["--query", "Find review papers on gut microbiota", "--max-results", "-1"])

    def test_download_request_stdin_downloads_pmc_pdf(self):
        download_request = {
            "selected_indices": [1],
            "download_context": {"topic": "Gut microbiota and Alzheimer's disease", "folder_name": "Gut microbiota and Alzheimer's disease"},
            "results": [
                {
                    "result_index": 1,
                    "title": "Gut microbiota and Alzheimer's disease in mice",
                    "pmid": "12345678",
                    "pmcid": "PMC1234567",
                    "doi": "10.1000/example-doi",
                }
            ],
        }
        temp_dir = ROOT / "_download_test_tmp_pmc"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
                 mock.patch.object(self.module, "request_with_retries", return_value=make_response(b"%PDF-1.4 test pdf", url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/pdf/")), \
                 mock.patch("sys.stdin", io.StringIO(json.dumps(download_request))):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = self.module.main(["--download-request-stdin", "--download-root", str(temp_dir)])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["downloaded_count"], 1)
            self.assertEqual(payload["failed_count"], 0)
            self.assertEqual(payload["downloaded"][0]["source"], "pmc")
            saved_path = pathlib.Path(payload["downloaded"][0]["file_path"])
            self.assertTrue(saved_path.exists())
            self.assertTrue(saved_path.read_bytes().startswith(b"%PDF"))
            self.assertEqual(stderr.getvalue(), "")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_download_request_reports_failure_when_no_configured_source_exists(self):
        download_request = {
            "selected_indices": [1],
            "download_context": {"topic": "Gut microbiota and Alzheimer's disease", "folder_name": "Gut microbiota and Alzheimer's disease"},
            "results": [
                {
                    "result_index": 1,
                    "title": "Gut microbiota and Alzheimer's disease in mice",
                    "pmid": "12345678",
                    "pmcid": "",
                    "doi": "",
                }
            ],
        }
        temp_dir = ROOT / "_download_test_tmp_fail"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            with mock.patch.object(self.module, "ensure_runtime_dependencies"), \
                 mock.patch.object(self.module, "request_json", return_value={"records": [{}]}), \
                 mock.patch.object(self.module, "request_with_retries", side_effect=self.module.RequestsRequestException("lookup blocked")), \
                 mock.patch("sys.stdin", io.StringIO(json.dumps(download_request))):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = self.module.main(["--download-request-stdin", "--download-root", str(temp_dir)])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["downloaded_count"], 0)
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("Gut microbiota and Alzheimer's disease in mice", payload["failed"][0]["title"])
        self.assertEqual(payload["failed"][0]["reason_code"], "no_pmc_pdf")
        self.assertIn("PMC did not provide a downloadable PDF", payload["failed"][0]["reason"])
        self.assertIn("PMC:", payload["failed"][0]["reason_detail"])
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
