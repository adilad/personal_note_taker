sqlite3 journal.db <<'EOF'
.headers on
.mode csv
.once segments.csv
SELECT * FROM segments ORDER BY id;
EOF

