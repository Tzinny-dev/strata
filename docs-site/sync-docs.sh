#!/usr/bin/env bash
# Sync docs/ to docs-site/guide/, docs-site/reference/, docs-site/spec/
# Run from project root: ./docs-site/sync-docs.sh

set -e

DOCS_DIR="docs"
GUIDE_DIR="docs-site/guide"
REF_DIR="docs-site/reference"
SPEC_DIR="docs-site/spec"

mkdir -p "$GUIDE_DIR" "$REF_DIR" "$SPEC_DIR"

# Guide files
cp "$DOCS_DIR/getting-started.md" "$GUIDE_DIR/"
cp "$DOCS_DIR/tutorial.md" "$GUIDE_DIR/"
cp "$DOCS_DIR/warehouse-semantics.md" "$GUIDE_DIR/"
cp "$DOCS_DIR/warehouse-adapters.md" "$GUIDE_DIR/"
cp "$DOCS_DIR/binary-standalone.md" "$GUIDE_DIR/"
cp "$DOCS_DIR/vscode.md" "$GUIDE_DIR/"

# Reference files
cp "$DOCS_DIR/syntax-reference.md" "$REF_DIR/"
cp "$DOCS_DIR/strict-contracts.md" "$REF_DIR/"
cp "$DOCS_DIR/incremental.md" "$REF_DIR/"
cp "$DOCS_DIR/setops.md" "$REF_DIR/"
cp "$DOCS_DIR/json-arrays.md" "$REF_DIR/"
cp "$DOCS_DIR/date-functions.md" "$REF_DIR/"
cp "$DOCS_DIR/join-cardinality.md" "$REF_DIR/"
cp "$DOCS_DIR/nested-domains.md" "$REF_DIR/"

# Spec files
cp "$DOCS_DIR/spec/grammar.md" "$SPEC_DIR/"
cp "$DOCS_DIR/spec/types-and-contracts.md" "$SPEC_DIR/"

echo "Synced docs/ to docs-site/guide/, docs-site/reference/, docs-site/spec/"