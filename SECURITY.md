# Security

## Scope of use

QAura drives a real browser against a target web application, including submitting forms, following links, and — with `--role` — acting under a captured authenticated session. Only point it at applications you are authorized to test. `guardrails.allowed_domains` constrains where the crawler will navigate, but it is a safety net, not a substitute for authorization.

## Loading model artifacts

`qaura ml test` and `qaura ml probe` load model files you point them at. For `.pkl`/`.joblib` files this goes through `joblib.load()`, and for `.pt`/`.pth` files through `torch.load(..., weights_only=False)` — both deserialize Python pickles, which can execute arbitrary code embedded in the file. This is inherent to how scikit-learn/joblib and PyTorch persist models; QAura does not sandbox it. Only run these commands against model artifacts you trust, the same way you wouldn't `pip install` an untrusted package without checking it first.

## Reporting a vulnerability

If you find a security issue in QAura itself (not in a target application it's testing), open an issue describing it. There is no dedicated security contact yet — flag it clearly as a security report in the title.
