# Changelog

## v0.3.0 - 2026-04-16

### Changed

- Improve multi-run CLI output with a `TOTAL` row in the top summary table and a rightmost `TOTAL` column in the product table.
- Standardize day labels in CLI summaries to use `D-2`, `D-1`, `D=0`, and `D+N` formatting.
- Add regression tests for the new CLI summary label and total-column behavior.

## v0.2.2 - 2026-04-14

### Added

- Bundle the raw IMC round 1 day CSVs under `datasets/round1/` with the official filenames from the latest round 1 download.
- Make round 1 the default zero-argument dataset once it is the latest populated bundled round.

### Changed

- Add `INTARIAN_PEPPER_ROOT` and `ASH_COATED_OSMIUM` to the default trader and position-limit map with round 1 limits of `80` each.
- Update the README to document the bundled round 1 data and default dataset behavior.

## v0.2.1 - 2026-04-12

### Fixed

- Remove market trades from persisted bundle timelines once they have been consumed by the submission, preventing duplicate replay data in generated artifacts.
- Add regression coverage for consumed market trades so the bundled timeline and submission log stay in sync.
- Serialize Python trader module loading so embedded `datamodel` imports do not fail under parallel test execution.
