/** Redaction before any local persistence; avoid recording credentials in archive files.
 * Same pattern set as openclaw/src/redact.ts, opencode/src/util/redact.ts and the Python
 * skills/token-optimizer/scripts/credential_patterns.py — keep all four in sync.
 * Entry: [pattern, replacement]. "$1" preserves a non-secret keep-group prefix
 * (parameter name, flag, context key) exactly like Python's `keep` group. */
const PATTERNS: [RegExp, string][] = [
  [/\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b/g, '[REDACTED]'],
  [/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]'],
  [/\bAKIA[0-9A-Z]{16}\b/g, '[REDACTED]'],
  [/\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b/gi, '[REDACTED]'],
  [/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, '[REDACTED]'],
  [/(?:[sr]k_live_|sk_test_)[A-Za-z0-9]{24,}/g, '[REDACTED]'],
  // Slack app-level token (xapp-) and incoming-webhook URL: both are live credentials.
  [/\bxapp-\d-[A-Z0-9]+-\d+-[A-Za-z0-9]+/g, '[REDACTED]'],
  [/https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9\/_-]+/g, '[REDACTED]'],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[REDACTED]'],
  [/\b(?:npm_[A-Za-z0-9]{36}|xox[bpa]-[0-9A-Za-z-]{20,}|xox[bpa]-[0-9]+-[A-Za-z0-9]+|hf_[A-Za-z0-9]{34}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9_-]{20,})\b/g, '[REDACTED]'],
  [/\bgl(?:pat|dt|rt|cbt|ptt|ft|imt|agent|soat)-[A-Za-z0-9_.-]{20,}/g, '[REDACTED]'],
  [/\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^:\s/]+:[^@\s]+@/gi, '[REDACTED]'],
  [/https?:\/\/[^:\s/@]+:[^@\s]+@/gi, '[REDACTED]'],
  [/([?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|session[_-]?token|id[_-]?token|api[_-]?key|sessionid|session|password|passwd|signature|secret|bearer|token|auth|sig|pwd|key|jwt)=)(?!\[CREDENTIAL REDACTED:)[^&#;\s"'<>]+/gi, '$1[REDACTED]'],
  // Generic assignment (F3): a name that ends in KEY/TOKEN/SECRET/PASSWORD/PASSWD/PWD/CREDENTIAL
  // (optionally plural, so tokenizer and keyboard stay readable) followed by = or :. Also covers
  // YAML labels and JSON keys. The VALUE is hidden whole, quoted values with spaces included; the
  // name and the surrounding quotes stay. Skipped: placeholders, small numbers, booleans, type
  // names, calls, $VAR refs, empty strings.
  [/((?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)(?:e?s)?(?![A-Za-z])(?=([A-Za-z0-9_.-]{0,80}))\2["']?[ \t]*[=:][ \t]*["']?)(?![=>:])(?!(?<=")"|(?<=')')(?!""|'')(?!\[(?:CREDENTIAL )?REDACTED)(?!-?\d{1,6}(?:[.,]\d+)?[kKmM%]?["']?(?![\w$]))(?!(?:true|false|yes|no|on|off|null|none|nil|undefined)["']?(?![\w$]))(?!(?:string|number|boolean|bool|str|int|float|any|unknown|object|void)(?![\w$]))(?![A-Za-z_][\w.]*[(\[])(?!\$[{(])(?!\$[A-Z_][A-Z0-9_]*(?![\w$]))(?:(?<=")[^"\n]+(?=")|(?<=')[^'\n]+(?=')|\S+)/gi, '$1[REDACTED]'],
  [/(\b(?:PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|MONGO_PASSWORD|DB_PASSWORD|DATABASE_PASSWORD|PGPASSWD)=["']?)(?!\[CREDENTIAL REDACTED:)[^\s"'\n]+/gi, '$1[REDACTED]'],
  [/(\b(?:aws_secret_access_key|aws_secret|secret_access_key|SecretAccessKey)["'\s:=]+)(?!\[CREDENTIAL REDACTED:)[A-Za-z0-9/+=]{40}/gi, '$1[REDACTED]'],
  [/((?:--password|--passwd|--passcode|--auth-token)(?![\w-])(?:\s*=\s*|\s+))(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/gi, '$1[REDACTED]'],
  // -p / -a flag letters are case-SENSITIVE in the source patterns (Python
  // (?-i:-p)); JS has no inline flag scoping, so the command names spell both
  // cases and the flags stay bare. mysql -p<inline>, mariadb/sshpass -p, redis-cli -a.
  [/(\b[Mm][Yy][Ss][Qq][Ll]\s+.*?(?<!\S)-p\s*)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, '$1[REDACTED]'],
  [/(\b(?:[Ss][Ss][Hh][Pp][Aa][Ss][Ss]|[Mm][Aa][Rr][Ii][Aa][Dd][Bb]).*?(?<!\S)-p\s*)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, '$1[REDACTED]'],
  [/(\b[Rr][Ee][Dd][Ii][Ss]-[Cc][Ll][Ii].*?(?<!\S)-a\s+)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, '$1[REDACTED]'],
];
export function redact(text: string): string {
  let result = text;
  for (const [pattern, replacement] of PATTERNS) result = result.replace(pattern, replacement);
  return result;
}
/** Sensitive labels are rejected, not redacted by guessing at their value shape. */
const SECRET_LABELS = [
  ["secret"], ["token"], ["password"], ["passwd"], ["credential"], ["credentials"],
  ["authorization"], ["cookie"],
  ["api", "key"], ["access", "key"], ["session", "key"], ["signing", "key"],
  ["private", "key"], ["client", "secret"], ["client", "id"],
  ["access", "token"], ["refresh", "token"], ["session", "token"], ["auth", "token"],
] as const;
function sensitiveLabel(value: string): boolean {
  // Preserve word boundaries: secretary, tokenizer, tokenized and authentication
  // are not secret labels. CamelCase keys such as myToken count as two words.
  const matches = (words: string[]) => SECRET_LABELS.some(label => words.some((_, i) => label.every((word, j) => words[i + j] === word)));
  const words = value.toLowerCase().split(/[\s_.-]+/).filter(Boolean);
  if (matches(words)) return true;
  // A second pass handles myToken without breaking mixed-case API labels.
  return matches(value.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().split(/[\s_.-]+/).filter(Boolean));
}
/** Unknown formats remain possible; opt-in storage is never a secret vault. */
export function suspiciousSecret(text: string): boolean {
  if (/-----BEGIN [^-]*PRIVATE KEY-----/i.test(text)) return true;
  const normalized = text.replace(/\s/gu, " ");
  // Key-value labels in environment files, YAML, JSON, logs, and prose.
  // Quoted/multiline/short values are caught by inspecting the label alone.
  const assignments = /(?:^|[ ,{])(?:["']?)([A-Za-z][A-Za-z0-9_.-]*(?:[ _.-]+[A-Za-z0-9_.-]+){0,5})["']? *[:=]/gm;
  for (const match of normalized.matchAll(assignments)) if (sensitiveLabel(match[1])) return true;
  // XML tags and bare label-value lines such as "password abc123...".
  for (const match of normalized.matchAll(/< *([A-Za-z][A-Za-z0-9_.-]*) *>/g)) if (sensitiveLabel(match[1])) return true;
  for (const line of text.split(/\r?\n/)) {
    const bare = line.replace(/\s/gu, " ").match(/(?:^|\bgoal: *)(password|passwd|api[ _.-]+key|access[ _.-]+key|session[ _.-]+key|signing[ _.-]+key|client[ _.-]+secret) +[^ :={}<]{8,}/i);
    if (bare && sensitiveLabel(bare[1])) return true;
  }
  if (/\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/.test(text)) return true;
  if (/[A-Za-z0-9_+/=-]{80,}/.test(text)) return true;
  return false;
}
