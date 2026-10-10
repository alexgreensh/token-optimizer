/**
 * Redaction hardening (F3 generic assignments, F5 vendor shapes, F6 linear-time JWT,
 * F8 Bearer). The same table runs in all three TS engines (openclaw, opencode, pi) so
 * they stay identical. Values are made up and fragments are joined at runtime, so no
 * literal here matches a real-secret shape.
 */
import { test, expect } from "bun:test";
import { redact } from "./redact";

type Vec = { name: string; input: string[]; absent?: string[]; present?: string[]; same?: boolean };
const VECTORS: Vec[] = [
  {
    "name": "F5 Stripe sk_test_ key",
    "input": [
      "pay with sk_te",
      "st_51FakeTestKeyZz9Qw7Lm3PaXy now"
    ],
    "absent": [
      "51FakeTestKeyZz9Qw7Lm3PaXy"
    ],
    "present": [
      "pay with ",
      " now"
    ]
  },
  {
    "name": "F5 Slack xapp app token",
    "input": [
      "slack xa",
      "pp-1-A0123456789-1234567890123-0123456789abcdef0123456789abcdef end"
    ],
    "absent": [
      "A0123456789",
      "0123456789abcdef"
    ],
    "present": [
      "slack ",
      " end"
    ]
  },
  {
    "name": "F5 Slack incoming webhook",
    "input": [
      "notify https://hooks.sl",
      "ack.com/services/T0FAKE0/B0FAKE0/FAKEhookTok9Qw7Lm3 now"
    ],
    "absent": [
      "FAKEhookTok9Qw7Lm3",
      "T0FAKE0/B0FAKE0"
    ],
    "present": [
      "notify ",
      " now"
    ]
  },
  {
    "name": "F5 webhook keeps the closing quote and paren",
    "input": [
      "curl -d x \"https://hooks.sl",
      "ack.com/services/T0FAKE0/B0FAKE0/FAKEhookTok9Qw7Lm3\") ok"
    ],
    "absent": [
      "FAKEhookTok9Qw7Lm3"
    ],
    "present": [
      "\") ok"
    ]
  },
  {
    "name": "F5 short sk_test_ is not a key",
    "input": [
      "sk_te",
      "st_short here"
    ],
    "same": true
  },
  {
    "name": "F5 other hooks.slack.com paths are not webhooks",
    "input": [
      "see https://hooks.sl",
      "ack.com/docs/intro for details"
    ],
    "same": true
  }
];

for (const v of VECTORS) {
  test(`openclaw redactor hardening: ${v.name}`, () => {
    const text = v.input.join("");
    const out = redact(text);
    if (v.same) {
      expect(out).toBe(text);
      return;
    }
    for (const needle of v.absent ?? []) expect(!out.includes(needle)).toBe(true);
    for (const needle of v.present ?? []) expect(out.includes(needle)).toBe(true);
  });
}
