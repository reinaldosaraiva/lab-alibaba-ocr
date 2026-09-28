- Células na matriz: 200/200
- Status: complete 139, partial 1, skipped 60
- Skipped (diff sem itens revisáveis): 60 (['axios--axios-11158', 'axios--axios-11164', 'axios--axios-11169', 'axios--axios-11188', 'axios--axios-11189', 'axios--axios-11212', 'axios--axios-11234', 'axios--axios-11244', 'gin-gonic--gin-4571', 'gin-gonic--gin-4678', 'gin-gonic--gin-4699', 'gin-gonic--gin-4807', 'gin-gonic--gin-4822', 'psf--requests-7386', 'psf--requests-7490'])

### Tokens e latência (runs complete)

| modelo | effort | n | tokens in (Σ) | tokens out (Σ) | tokens (Σ) | wall mediana (s) | wall média (s) | wall p95 (s) |
|--------|--------|---|---------------|----------------|------------|------------------|---------------|--------------|
| qwen38-27b | medium | 35 | 2,680,312 | 257,937 | 2,938,249 | 43 | 128 | 1020 |
| qwen38-27b | high | 34 | 2,828,309 | 244,237 | 3,072,546 | 62 | 125 | 611 |
| deepseek-v4-pro | medium | 35 | 4,062,982 | 423,756 | 4,486,738 | 130 | 195 | 756 |
| deepseek-v4-pro | high | 35 | 4,320,129 | 451,903 | 4,772,032 | 115 | 210 | 796 |

### Findings e ancoragem (runs complete)

| modelo | effort | n runs | findings (Σ) | ancorados (Σ) | ancoragem | findings/run |
|--------|--------|--------|--------------|---------------|-----------|--------------|
| qwen38-27b | medium | 35 | 25 | 24 | 96.0% | 0.71 |
| qwen38-27b | high | 34 | 24 | 24 | 100.0% | 0.71 |
| deepseek-v4-pro | medium | 35 | 65 | 65 | 100.0% | 1.86 |
| deepseek-v4-pro | high | 35 | 52 | 52 | 100.0% | 1.49 |

### Custo

- **$0.00 pay-as-you-go** — os dois endpoints são planos de assinatura (Magalu Coding Plan + Bailian Token Plan); o consumo é medido em tokens (colunas acima). Teto USD pré-registrado: $0.00 (DR5 item 2).
- Runs com `budget_exceeded` (1M tok/MR): 0
