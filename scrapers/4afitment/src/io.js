import fs from "node:fs";
import path from "node:path";

export function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

export function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export function appendJsonLine(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(value)}\n`, "utf8");
}

export function csvEscape(value) {
  const text = value == null ? "" : String(value);
  if (!/[",\r\n]/.test(text)) return text;
  return `"${text.replaceAll('"', '""')}"`;
}

export function writeCsv(file, rows) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const header = ["year", "manufacturer", "model"];
  const lines = [header.join(",")];
  for (const row of rows) {
    lines.push(header.map((key) => csvEscape(row[key])).join(","));
  }
  fs.writeFileSync(file, `${lines.join("\n")}\n`, "utf8");
}

export function tsvEscape(value) {
  return String(value ?? "")
    .replace(/\r?\n/g, " ")
    .replaceAll("\t", " ")
    .trim();
}

export function parseCopiedTsv(text) {
  const rows = [];

  for (const line of String(text ?? "").split(/\r?\n/)) {
    if (!line.trim()) continue;

    const cells = line.split("\t").map((cell) => cell.trim());
    while (cells.length && !cells[cells.length - 1]) cells.pop();
    if (cells.length < 3 || !/^\d{4}$/.test(cells[0])) continue;

    rows.push({
      year: cells[0],
      make: cells[1],
      model: cells[2]
    });
  }

  return rows;
}

export function ensureTsv(file, header) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const expectedHeader = header.map(tsvEscape).join("\t");
  if (fs.existsSync(file) && fs.statSync(file).size > 0) {
    const handle = fs.openSync(file, "r");
    try {
      const buffer = Buffer.alloc(256);
      const bytesRead = fs.readSync(handle, buffer, 0, buffer.length, 0);
      const actualHeader = buffer.subarray(0, bytesRead).toString("utf8").split(/\r?\n/, 1)[0];
      if (actualHeader !== expectedHeader) {
        throw new Error(`已有 TSV 表头不兼容：${file}（${actualHeader}）`);
      }
    } finally {
      fs.closeSync(handle);
    }
    return;
  }
  fs.writeFileSync(file, `${expectedHeader}\n`, "utf8");
}

export function appendTsv(file, header, rows) {
  if (!rows.length) return;
  ensureTsv(file, header);
  const lines = rows.map((row) => header.map((key) => tsvEscape(row[key])).join("\t"));
  fs.appendFileSync(file, `${lines.join("\n")}\n`, "utf8");
}
