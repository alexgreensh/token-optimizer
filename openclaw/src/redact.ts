/** Redaction before checkpoint persistence; checkpoints must never store credentials.
 * Same pattern set as pi/src/redact.ts, opencode/src/util/redact.ts and the Python
 * skills/token-optimizer/scripts/credential_patterns.py — keep all four in sync.
 * Entry: [pattern, replacement]. "$1" preserves a non-secret keep-group prefix
 * (parameter name, flag, context key) exactly like Python's `keep` group. */
const PATTERNS: [RegExp, string][] = [
  [/\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b/g, "[REDACTED]"],
  [/\bsk-[A-Za-z0-9_-]{20,}\b/g, "[REDACTED]"],
  [/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED]"],
  [/\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b/gi, "[REDACTED]"],
  [/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, "[REDACTED]"],
  [/(?:[sr]k_live_|sk_test_)[A-Za-z0-9]{24,}/g, "[REDACTED]"],
  // Slack app-level token (xapp-) and incoming-webhook URL: both are live credentials.
  [/\bxapp-\d-[A-Z0-9]+-\d+-[A-Za-z0-9]+/g, "[REDACTED]"],
  [/https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9\/_-]+/g, "[REDACTED]"],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, "[REDACTED]"],
  [/\b(?:npm_[A-Za-z0-9]{36}|xox[bpa]-[0-9A-Za-z-]{20,}|xox[bpa]-[0-9]+-[A-Za-z0-9]+|hf_[A-Za-z0-9]{34}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9_-]{20,})\b/g, "[REDACTED]"],
  [/\bgl(?:pat|dt|rt|cbt|ptt|ft|imt|agent|soat)-[A-Za-z0-9_.-]{20,}/g, "[REDACTED]"],
  [/\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^:\s/]+:[^@\s]+@/gi, "[REDACTED]"],
  [/https?:\/\/[^:\s/@]+:[^@\s]+@/gi, "[REDACTED]"],
  [/([?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|session[_-]?token|id[_-]?token|api[_-]?key|sessionid|session|password|passwd|signature|secret|bearer|token|auth|sig|pwd|key|jwt)=)(?!\[CREDENTIAL REDACTED:)[^&#;\s"'<>]+/gi, "$1[REDACTED]"],
  // Generic assignment (F3): a name that ends in KEY/TOKEN/SECRET/PASSWORD/PASSWD/PWD/CREDENTIAL
  // (optionally plural, so tokenizer and keyboard stay readable) followed by = or :. Also covers
  // YAML labels and JSON keys. The VALUE is hidden whole, quoted values with spaces included; the
  // name and the surrounding quotes stay. Skipped: placeholders, small numbers, booleans, type
  // names, calls, $VAR refs, empty strings.
  [/((?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)(?:e?s)?(?![A-Za-z])(?=([A-Za-z0-9_.-]{0,80}))\2["']?[ \t]*[=:][ \t]*["']?)(?![=>:])(?!(?<=")"|(?<=')')(?!""|'')(?!\[(?:CREDENTIAL )?REDACTED)(?!-?\d{1,6}(?:[.,]\d+)?[kKmM%]?["']?(?![\w$]))(?!(?:true|false|yes|no|on|off|null|none|nil|undefined)["']?(?![\w$]))(?!(?:string|number|boolean|bool|str|int|float|any|unknown|object|void)(?![\w$]))(?![A-Za-z_][\w.]*[(\[])(?!\$[{(])(?!\$[A-Z_][A-Z0-9_]*(?![\w$]))(?:(?<=")[^"\n]+(?=")|(?<=')[^'\n]+(?=')|\S+)/gi, "$1[REDACTED]"],
  [/(\b(?:PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|MONGO_PASSWORD|DB_PASSWORD|DATABASE_PASSWORD|PGPASSWD)=["']?)(?!\[CREDENTIAL REDACTED:)[^\s"'\n]+/gi, "$1[REDACTED]"],
  [/(\b(?:aws_secret_access_key|aws_secret|secret_access_key|SecretAccessKey)["'\s:=]+)(?!\[CREDENTIAL REDACTED:)[A-Za-z0-9/+=]{40}/gi, "$1[REDACTED]"],
  [/((?:--password|--passwd|--passcode|--auth-token)(?![\w-])(?:\s*=\s*|\s+))(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/gi, "$1[REDACTED]"],
  // -p / -a flag letters are case-SENSITIVE in the source patterns (Python
  // (?-i:-p)); JS has no inline flag scoping, so the command names spell both
  // cases and the flags stay bare. mysql -p<inline>, mariadb/sshpass -p, redis-cli -a.
  [/(\b[Mm][Yy][Ss][Qq][Ll]\s+.*?(?<!\S)-p\s*)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, "$1[REDACTED]"],
  [/(\b(?:[Ss][Ss][Hh][Pp][Aa][Ss][Ss]|[Mm][Aa][Rr][Ii][Aa][Dd][Bb]).*?(?<!\S)-p\s*)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, "$1[REDACTED]"],
  [/(\b[Rr][Ee][Dd][Ii][Ss]-[Cc][Ll][Ii].*?(?<!\S)-a\s+)(?!-)(?!\[CREDENTIAL REDACTED:)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/g, "$1[REDACTED]"],
];
export function redact(text: string): string {
  let result = text;
  for (const [pattern, replacement] of PATTERNS) result = result.replace(pattern, replacement);
  return result;
}
