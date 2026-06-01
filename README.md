Js Enpoint Extract is a multi threaded Python tool that scans JavaScript files for exposed API endpoints and URL paths. Feed it a single JS URL or a bulk list, and it runs regex patterns against 
each file's content to surface hidden routes, useful for recon and attack surface mapping during web security assessments.

Results can be saved as plain text or JSON, with built-in noise filtering to cut false positives, retry logic for flaky servers, and optional global deduplication so the same endpoint 
isn't reported twice across multiple files.
