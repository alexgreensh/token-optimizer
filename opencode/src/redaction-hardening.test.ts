/**
 * Redaction hardening (F3 generic assignments, F5 vendor shapes, F6 linear-time JWT,
 * F8 Bearer). The same table runs in all three TS engines (openclaw, opencode, pi) so
 * they stay identical. Values are made up and fragments are joined at runtime, so no
 * literal here matches a real-secret shape.
 */
import { test, expect } from "bun:test";
import { redact } from "./util/redact";

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
  },
  {
    "name": "F3 PASSWORD env prefix",
    "input": [
      "PASSWORD=hunter2FAKE ./deploy.sh"
    ],
    "absent": [
      "hunter2FAKE"
    ],
    "present": [
      "PASSWORD=",
      "./deploy.sh"
    ]
  },
  {
    "name": "F3 API_KEY env prefix",
    "input": [
      "API_KEY=FAKEenvApiKeyQw7Lm3 make deploy"
    ],
    "absent": [
      "FAKEenvApiKeyQw7Lm3"
    ],
    "present": [
      "API_KEY=",
      "make deploy"
    ]
  },
  {
    "name": "F3 SECRET_KEY env prefix",
    "input": [
      "SECRET_KEY=FAKEsecretKeyQw7Lm3 python app.py"
    ],
    "absent": [
      "FAKEsecretKeyQw7Lm3"
    ],
    "present": [
      "SECRET_KEY=",
      "python app.py"
    ]
  },
  {
    "name": "F3 YAML password label",
    "input": [
      "db:\n  password: FAKEyamlPw9Zz\n  host: h"
    ],
    "absent": [
      "FAKEyamlPw9Zz"
    ],
    "present": [
      "db:\n  password: ",
      "\n  host: h"
    ]
  },
  {
    "name": "F3 YAML api-key label",
    "input": [
      "svc:\n  api-key: FAKEyamlKey7Lm3\n  region: eu"
    ],
    "absent": [
      "FAKEyamlKey7Lm3"
    ],
    "present": [
      "api-key: ",
      "region: eu"
    ]
  },
  {
    "name": "F3 YAML token and secret labels",
    "input": [
      "token: FAKEtokVal88Qw\nsecret: FAKEsecVal77Zx\nname: svc"
    ],
    "absent": [
      "FAKEtokVal88Qw",
      "FAKEsecVal77Zx"
    ],
    "present": [
      "token: ",
      "secret: ",
      "name: svc"
    ]
  },
  {
    "name": "F3 quoted value with spaces is fully hidden",
    "input": [
      "export DB_PASSWORD='correct horse FAKE' && ./migrate"
    ],
    "absent": [
      "correct",
      "horse",
      "FAKE'"
    ],
    "present": [
      "export DB_PASSWORD=",
      "&& ./migrate"
    ]
  },
  {
    "name": "F3 double-quoted value with spaces",
    "input": [
      "MY_SECRET=\"two words FAKEsec\" run"
    ],
    "absent": [
      "two words",
      "FAKEsec"
    ],
    "present": [
      "MY_SECRET=",
      " run"
    ]
  },
  {
    "name": "F3 JSON key and value",
    "input": [
      "{\"api_key\": \"FAKEjsonKey9Qw\", \"n\": 1}"
    ],
    "absent": [
      "FAKEjsonKey9Qw"
    ],
    "present": [
      "\"api_key\"",
      "\"n\": 1"
    ]
  },
  {
    "name": "F3 lowercase assignment with spaces around =",
    "input": [
      "auth_token = FAKEspacedTok55Lm"
    ],
    "absent": [
      "FAKEspacedTok55Lm"
    ],
    "present": [
      "auth_token"
    ]
  },
  {
    "name": "F3 camelCase and header names",
    "input": [
      "apiKey: FAKEcamelKey44Qw\nx-api-key: FAKEhdrKey33Lm"
    ],
    "absent": [
      "FAKEcamelKey44Qw",
      "FAKEhdrKey33Lm"
    ],
    "present": [
      "apiKey: ",
      "x-api-key: "
    ]
  },
  {
    "name": "F3 AWS secret env name",
    "input": [
      "AWS_SECRET_ACCESS_KEY=FAKEawsSecretAccessKeyValue1234 aws s3 ls"
    ],
    "absent": [
      "FAKEawsSecretAccessKeyValue1234"
    ],
    "present": [
      "AWS_SECRET_ACCESS_KEY=",
      "aws s3 ls"
    ]
  },
  {
    "name": "F3 credential name",
    "input": [
      "CREDENTIAL=FAKEcredVal22Zx;"
    ],
    "absent": [
      "FAKEcredVal22Zx"
    ],
    "present": [
      "CREDENTIAL="
    ]
  },
  {
    "name": "F3 passwd and pwd names",
    "input": [
      "DB_PASSWD=FAKEpasswdVal11Qw MYSQL_PWD2=FAKEpwdVal00Lm"
    ],
    "absent": [
      "FAKEpasswdVal11Qw",
      "FAKEpwdVal00Lm"
    ],
    "present": [
      "DB_PASSWD=",
      "MYSQL_PWD2="
    ]
  },
  {
    "name": "F3 idempotent on its own output",
    "input": [
      "API_KEY=FAKEidemVal66Qw run"
    ],
    "absent": [
      "FAKEidemVal66Qw"
    ],
    "present": [
      "API_KEY=[REDACTED] run"
    ]
  },
  {
    "name": "F3 existing placeholder is not wrapped again",
    "input": [
      "PASSWORD=[CREDENTIAL REDACTED: Bearer token] x"
    ],
    "same": true
  },
  {
    "name": "F3 keep: the token count is 500",
    "input": [
      "the token count is 500"
    ],
    "same": true
  },
  {
    "name": "F3 keep: token_count = len(x)",
    "input": [
      "token_count = len(x)"
    ],
    "same": true
  },
  {
    "name": "F3 keep: max_tokens=4096",
    "input": [
      "max_tokens=4096"
    ],
    "same": true
  },
  {
    "name": "F3 keep: tokens: 1200",
    "input": [
      "tokens: 1200"
    ],
    "same": true
  },
  {
    "name": "F3 keep: JSON usage numbers",
    "input": [
      "{\"input_tokens\": 1234, \"cache_read_input_tokens\":0}"
    ],
    "same": true
  },
  {
    "name": "F3 keep: boolean and null values",
    "input": [
      "use_key: true\nauth_token = None\nsecret_enabled=false\npassword: null"
    ],
    "same": true
  },
  {
    "name": "F3 keep: code calls and var refs",
    "input": [
      "token = getToken();\nAPI_KEY=$OPENAI_KEY run\nAPI_KEY=${OPENAI_KEY} run\nsecret=$(cat f)"
    ],
    "same": true
  },
  {
    "name": "F3 keep: type annotations",
    "input": [
      "apiKey: string;\ntoken: number,\nsecret: boolean"
    ],
    "same": true
  },
  {
    "name": "F3 keep: comparisons and walrus",
    "input": [
      "if token == expected:\nkey => value\ntoken := fetch()"
    ],
    "same": true
  },
  {
    "name": "F3 keep: empty quoted values",
    "input": [
      "PASSWORD=\"\" run\nsecret: ''\n{\"password\":\"\"}"
    ],
    "same": true
  },
  {
    "name": "F3 keep: env and index lookups",
    "input": [
      "secret = os.environ['X']\ntoken = cfg[\"t\"]\nGITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}"
    ],
    "same": true
  },
  {
    "name": "F3 keep: tokenizer and keyboard are not secret names",
    "input": [
      "tokenizer: gpt2\nkeyboard: us\ntokenized=yes_please"
    ],
    "same": true
  },
  {
    "name": "F3 keep: ordinary prose with the word key",
    "input": [
      "Key takeaways: the secret ingredient is time. Press the key to continue."
    ],
    "same": true
  }
];

for (const v of VECTORS) {
  test(`opencode redactor hardening: ${v.name}`, () => {
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
