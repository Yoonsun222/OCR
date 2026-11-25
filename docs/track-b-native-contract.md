# Track B Native Contract

## Supported

- UTF-8 and UTF-8-BOM CSV as literal cells.
- XLSX shared strings, inline strings, numbers, booleans, formula text, and stored formula results.
- Worksheet identity and visibility, A1 locators, merged ranges, hidden rows and columns, filter ranges, and structured table ranges.
- Native chart titles, plot types, ordered series, legend labels, and explicit same-workbook source ranges.
- Exact cell lookup and sum, minimum, maximum, and average over fully covered ranges of supported finite numeric text. Answers cite at most three native evidence records.

## Rejected or non-executed

- Macros, OLE and other active attachments, external relationships, workbook links, and data connections. The parser never executes or refreshes workbook content.
- Formula evaluation. Stored results are labeled `cached_not_recalculated`. Missing results remain missing.
- Password-protected, malformed, over-limit, traversal, and high-compression-ratio packages.
- Combined plots with multiple plot groups and unsupported chart reference forms. Unsupported relations omit the link and record a fixed reason.
- Image coordinates, raster chart interpretation, forecasts, engineering inference, and unsupported natural-language calculations.

## Security behavior

Inputs are read locally from bounded packages. Evidence carries the source hash and original worksheet coordinate or object ID. The common query boundary must authorize evidence and recheck permission before releasing an answer. Routine logs exclude filenames, labels, values, formulas, and chart titles.

## Synthetic verification

Run `py -3.11 -m pytest tests/contracts tests/track_b -q` from the repository root. The golden cases cover real synthetic package parsing, evidence conversion, a lookup, an aggregate, a chart relation and a missing-cache abstention.

Accuracy measures exact matches among observations marked independent. Abstention precision counts correct abstentions among predicted abstentions, and is null if there are none. Include both answerable and unanswerable cases in that cohort. Repeated measurements of one case do not add another independent observation. Single-fixture timing is a regression smoke check. The approved company data and workstation are needed to measure production accuracy, P95 latency, memory, and confidence intervals.
