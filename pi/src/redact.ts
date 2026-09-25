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
