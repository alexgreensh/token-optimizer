/** Redaction before any local persistence; avoid recording credentials in archive files. */
const PATTERNS: RegExp[] = [
  /\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b/g,
  /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b/gi,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g,
  /\b(?:npm_[A-Za-z0-9]{36}|xox[bpa]-[0-9A-Za-z-]{20,}|hf_[A-Za-z0-9]{34}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9_-]{20,})\b/g,
  /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^:\s/]+:[^@\s]+@/gi,
  /https?:\/\/[^:\s/@]+:[^@\s]+@/gi,
  /([?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|session[_-]?token|api[_-]?key|password|passwd|signature|secret|bearer|token|auth|sig|pwd|key|jwt)=)[^&#;\s"'<>]+/gi,
  /\b(?:PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|MONGO_PASSWORD|DB_PASSWORD|DATABASE_PASSWORD|PGPASSWD)=[^\s"'\n]+/gi,
  /(?:--password|--passwd|--passcode|--auth-token)(?:\s*=\s*|\s+)(?:"[^"\n]*"|'[^'\n]*'|[^\s"']+)/gi,
];
export function redact(text: string): string {
  let result = text;
  for (const pattern of PATTERNS) result = result.replace(pattern, '[REDACTED]');
  return result;
}
/** Sensitive labels are rejected, not redacted by guessing at their value shape. */
const SECRET_LABELS = [
  ["secret"], ["token"], ["password"], ["passwd"], ["credential"], ["credentials"],
  ["api", "key"], ["access", "key"], ["session", "key"], ["signing", "key"],
  ["private", "key"], ["client", "secret"], ["client", "id"],
  ["access", "token"], ["refresh", "token"], ["session", "token"], ["auth", "token"],
] as const;
function sensitiveLabel(value: string): boolean {
  // Preserve word boundaries: secretary, tokenizer, tokenized and authentication
  // are not secret labels. CamelCase keys such as myToken count as two words.
  const words = value.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().split(/[\s_-]+/).filter(Boolean);
  return SECRET_LABELS.some(label => words.some((_, i) => label.every((word, j) => words[i + j] === word)));
}
/** Unknown formats remain possible; opt-in storage is never a secret vault. */
export function suspiciousSecret(text: string): boolean {
  if (/-----BEGIN [^-]*PRIVATE KEY-----/i.test(text)) return true;
  // Key-value labels in environment files, YAML, JSON, logs, and prose.
  // Quoted/multiline/short values are caught by inspecting the label alone.
  const assignments = /(?:^|[\s,{])(?:["']?)([A-Za-z][A-Za-z0-9_-]*(?:[ \t_-]+[A-Za-z0-9_-]+){0,5})["']?[ \t]*[:=]/gm;
  for (const match of text.matchAll(assignments)) if (sensitiveLabel(match[1])) return true;
  // XML tags and bare label-value lines such as "password abc123...".
  for (const match of text.matchAll(/<\s*([A-Za-z][A-Za-z0-9_-]*)\s*>/g)) if (sensitiveLabel(match[1])) return true;
  for (const line of text.split(/\r?\n/)) {
    const bare = line.match(/(?:^|\bgoal:[ \t]*)(password|passwd|api[ _-]+key|access[ _-]+key|session[ _-]+key|signing[ _-]+key|client[ _-]+secret)[ \t]+[^\s:={}<]{8,}/i);
    if (bare && sensitiveLabel(bare[1])) return true;
  }
  if (/\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/.test(text)) return true;
  if (/[A-Za-z0-9_+/=-]{80,}/.test(text)) return true;
  return false;
}
