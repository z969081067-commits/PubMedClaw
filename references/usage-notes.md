# Usage Notes

- Build PubMed queries from concept groups joined by `AND`, with synonym variants joined by `OR` inside each group.
- Prefer `[Title/Abstract]` terms for user-specified concepts unless a clear publication type or species MeSH filter exists.
- Treat explicit article-type CLI flags as filters. Treat softer language such as "prioritize reviews" as ranking boosts unless the request clearly asks for only that article type.
- If a request says "recent" without years, use a conservative recent window and document the assumption.
- If the request is ambiguous, keep the search broad enough to avoid zero-result failures and record the assumptions in the returned JSON and chat response.
- Rank results transparently:
  - Relevance first from keyword-group matches in title and abstract.
  - Then article-type match.
  - Then recency.
  - Then species match.
- If PubMed returns zero results, suggest broadening by removing publication type filters, removing species filters, or widening the date range.
- Number returned results in the same order as the ranked JSON so the user can later refer to paper numbers unambiguously.
- For download requests, use the numbered `result_index` values from the prior search payload rather than re-ranking or re-querying.
- Download flow should use PMC only.
- If PMC download fails, report the title plus a stable failure summary and detail back to the user instead of silently skipping it.
