/** Redaction before any local persistence; avoid recording credentials in archive files. */
const PATTERNS: RegExp[] = [
  /\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b/g,
  /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b/gi,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g,
];
export function redact(text: string): string {
  let result = text;
  for (const pattern of PATTERNS) result = result.replace(pattern, '[REDACTED]');
  return result;
}
