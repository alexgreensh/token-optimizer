/** The generic assignment rule (see the comment in PATTERNS). ASSIGNMENT matches the name, separator and
 * opening quote and checks the value only with zero-width guards; VALUE then reads the value, and only when it
 * is going to be hidden. A bare KEY before a colon is skipped without reading its value, so a long run of them
 * stays linear, and the scan resumes right after the name so an assignment inside the value is still found. */
const ASSIGNMENT = /((?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)(?:e?s)?(?![A-Za-z])(?=([A-Za-z0-9_.-]{0,80}))\2["']?[ \t]*[=:][ \t]*["']?)(?![=>:])(?!(?<=")"|(?<=')')(?!""|'')(?!\[(?:CREDENTIAL )?REDACTED)(?!-?\d{1,6}(?:[.,]\d+)?[kKmM%]?["']?(?![\w$]))(?!(?:true|false|yes|no|on|off|null|none|nil|undefined)["']?(?![\w$]))(?!(?:string|number|boolean|bool|str|int|float|any|unknown|object|void)(?![\w$]))(?![A-Za-z_][\w.]*[(\[])(?!\$[{(])(?!\$[A-Z_][A-Z0-9_]*(?![\w$]))/gi;
const ASSIGNMENT_VALUE = /(?:(?<=")[^"\n]+(?=")|(?<=')[^'\n]+(?=')|\S+)/y;
function redactAssignments(text: string): string {
  ASSIGNMENT.lastIndex = 0;
  let out = "";
  let last = 0;
  for (let m = ASSIGNMENT.exec(text); m !== null; m = ASSIGNMENT.exec(text)) {
    const end = m.index + m[0].length;
    const before = text.charAt(m.index - 1);
    const bareKeyBeforeColon = /^key/i.test(m[0]) && /:[ \t]*["']?$/.test(m[0])
      && !/[_-]/.test(before) && !(m[0].charAt(0) === "K" && /[a-z]/.test(before));
    if (bareKeyBeforeColon) continue;
    ASSIGNMENT_VALUE.lastIndex = end;
    const value = ASSIGNMENT_VALUE.exec(text);
    if (value === null) continue;
    out += text.slice(last, end) + "[REDACTED]";
    last = end + value[0].length;
    ASSIGNMENT.lastIndex = last;
  }
  return out + text.slice(last);
}
/** Redaction before checkpoint persistence; checkpoints must never store credentials.
 * Same pattern set as pi/src/redact.ts, opencode/src/util/redact.ts and the Python
 * skills/token-optimizer/scripts/credential_patterns.py — keep all four in sync.
 * Entry: [pattern, replacement]. "$1" preserves a non-secret keep-group prefix
 * (parameter name, flag, context key) exactly like Python's `keep` group. */
const PATTERNS: ([RegExp, string] | ((text: string) => string))[] = [
  [/\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b/g, "[REDACTED]"],
  [/\bsk-[A-Za-z0-9_-]{20,}\b/g, "[REDACTED]"],
  [/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED]"],
  // Bearer keeps its 16-character floor. The lookahead asks for a digit, a token punctuation mark
  // or an interior capital, or be 24+ plain letters (no English word is that long), so a long
  // plain word after "bearer" is not a credential.
  [/\b[Bb][Ee][Aa][Rr][Ee][Rr]\s+(?=[A-Za-z0-9._~+/=-]*(?:[0-9._~+/=-]|[A-Za-z][A-Z])|[A-Za-z]{24})[A-Za-z0-9._~+/=-]{16,}\b/g, "[REDACTED]"],
  // The lookbehind keeps this linear: without it every eyJ inside a long base64url run
  // restarts a scan to the end of the run (about 8 s at 100 KB).
  [/(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, "[REDACTED]"],
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
  // KEY before a COLON is an ordinary word in code (React key: item.id, "key": "user_id", primary key: id),
  // so there it counts only inside a compound name: _key / -key, or camelCase (apiKey, privateKey).
  // With = nothing changes. TOKEN/SECRET/PASSWORD/PASSWD/PWD/CREDENTIAL are unchanged for both.
  redactAssignments,
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
  for (const step of PATTERNS) result = typeof step === "function" ? step(result) : result.replace(step[0], step[1]);
  return result;
}
