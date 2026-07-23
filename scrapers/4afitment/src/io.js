import fs from "node:fs";
import path from "node:path";

export function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

const RETRYABLE_WRITE_ERROR_CODES = new Set([
  "EACCES",
  "EBUSY",
  "EMFILE",
  "ENFILE",
  "EPERM",
  "UNKNOWN"
]);

export function writeJson(file, value, { attempts = 8, retryDelayMs = 50 } = {}) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const content = `${JSON.stringify(value, null, 2)}\n`;
  let lastError;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const temporaryFile = path.join(
      path.dirname(file),
      `.${path.basename(file)}.${process.pid}.${Date.now()}.${attempt}.tmp`
    );

    try {
      const handle = fs.openSync(temporaryFile, "wx");
      try {
        fs.writeFileSync(handle, content, "utf8");
        fs.fsyncSync(handle);
      } finally {
        fs.closeSync(handle);
      }

      fs.renameSync(temporaryFile, file);
      return;
    } catch (error) {
      lastError = error;
      try {
        fs.rmSync(temporaryFile, { force: true });
      } catch {
        // A scanner may briefly hold the temporary file too; the next run can ignore it.
      }

      if (!RETRYABLE_WRITE_ERROR_CODES.has(error?.code) || attempt === attempts) {
        throw error;
      }

      sleepSync(retryDelayMs * attempt);
    }
  }

  throw lastError;
}

export function normalizeCheckpointQueues(completed, failed, invalidFailed) {
  for (const key of completed) {
    failed.delete(key);
    invalidFailed?.delete(key);
  }
  if (invalidFailed) {
    for (const key of failed) {
      invalidFailed.delete(key);
    }
  }
}

export function moveUnknownFailedToInvalid(
  failed,
  invalidFailed,
  modelsByManufacturer,
  manufacturers = Object.keys(modelsByManufacturer)
) {
  const knownManufacturers = new Set(
    manufacturers.map((manufacturer) => (
      typeof manufacturer === "string" ? manufacturer : manufacturer.text
    ))
  );
  const knownKeys = new Set();
  for (const [manufacturer, models] of Object.entries(modelsByManufacturer)) {
    if (!knownManufacturers.has(manufacturer)) continue;
    for (const model of models) {
      knownKeys.add(`${manufacturer}\t${model.text}`);
    }
  }

  for (const key of failed) {
    if (!knownKeys.has(key)) {
      failed.delete(key);
      invalidFailed.add(key);
    }
  }
}

function sleepSync(milliseconds) {
  const signal = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(signal, 0, 0, milliseconds);
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
