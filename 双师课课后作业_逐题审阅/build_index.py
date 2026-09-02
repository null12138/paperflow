from pathlib import Path
import re, json

root = Path(__file__).parent
text = (root / "original_text.txt").read_text(encoding="utf-8", errors="replace")
pages = text.split("\f")
records = []
for page_no, page in enumerate(pages, 1):
    lines = [re.sub(r"\s+", " ", x).strip() for x in page.splitlines()]
    lines = [x for x in lines if x]
    for i, line in enumerate(lines):
        if "【答案】" in line:
            m = re.search(r"(?<!\d)(\d{1,2})\s*[\.．]?\s*【答案】", line)
            qno = int(m.group(1)) if m else None
            records.append({
                "seq": len(records) + 1,
                "page": page_no,
                "qno": qno,
                "answer_line": line,
                "context_before": lines[max(0, i-8):i],
                "context_after": lines[i+1:i+8],
            })

(root / "question_index.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
with (root / "question_index.md").open("w", encoding="utf-8") as f:
    f.write("# 题目索引（按答案出现顺序）\n\n")
    f.write("|序号|答案页|原题号|答案行|\n|---:|---:|---:|---|\n")
    for r in records:
        ans = r["answer_line"].replace("|", "\\|")
        f.write(f'|{r["seq"]}|{r["page"]}|{r["qno"] or "?"}|{ans}|\n')

print(f"pages={len(pages)} answer_records={len(records)}")
