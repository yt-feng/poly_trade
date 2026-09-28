# Finite-bankroll scenario arithmetic

Public generic mathematical documentation only. No empirical results, selected addresses, private research logs or personal key material.

## Scope
`tools/finite_budget_math.py` checks a SUPPLIED net-return distribution and supplied legal all-in stakes. It has no order, account, fitting or network interface. The maximum calculated utility is not a recommendation, market probability estimate, canary approval or proof of economic edge. Caller-provided legal stakes and cash are not authenticated.

For stake fraction f and net returns R, the one-cycle quantities are E[cash PnL]=stake*E[R] and E[log change]=E[log(1+fR)]. A positive first quantity need not imply a positive second. If legal minimum stakes exceed the growth-optimal amount, rounding upward can make log growth negative. Compare zero risk explicitly rather than silently increasing risk. Net returns must include fees and other attributable costs exactly once.

For two supplied outcomes, +b and -l, and success probability p, the continuous unlevered Kelly fraction is clip((p*b-(1-p)*l)/(b*l),0,1). At a fixed nonzero f the zero-log success threshold is -log(1-f*l)/(log(1+f*b)-log(1-f*l)). Possible full loss with full capital has negative-infinite log expectation unless its probability is zero. This binary formula does not apply to a strategy whose actual exit distribution has other outcomes.

`minimum_bankroll_boundary` finds the boundary where a supplied minimum stake changes the sign of expected log growth. It is NOT a recommendation to deposit more: incorrect probabilities or a negative economic edge are not cured by larger capital. Positive-growth feasibility and optimal risk are different concepts.

Each scenario includes cash_cycle_seconds: a complete cycle through usable cash, including unsuccessful orders, holding and settlement/reconciliation. E[log change]/E[cycle seconds] is a renewal ratio ONLY under matching regenerative/stationary assumptions. It is not a forecast for correlated/nonstationary markets or frozen pending cash. Unknown return or unknown release time is rejected, not filled with zero. No automatic annualization or Monte Carlo return prediction is supplied.

## Private use
After registering a separately verified recipient, create a JSON outside the repository with equity, available_cash, minimum_stake, all_in_cap, legal_stakes, states, and net_costs_included=true. Each state has probability, net_return, cash_cycle_seconds. Values describe your chosen scenario, not assumed exchange rules.

```bash
python3 tools/finite_budget_math.py \
  --input "$HOME/.config/poly-research-vault/scenario.json" \
  --recipient research_vault/recipient.json \
  --recipient-id '<independently verified public fingerprint>' \
  --output "$HOME/.config/poly-research-vault/scenario.vault"
```

The recipient is checked before reading private inputs. Outputs are encrypted with the existing vault implementation; no plaintext result is printed. Missing keys stop the operation. This command does not initialize a personal identity or change any trading plan, TP rule, risk cap or running workflow. Keep all private observations and outputs out of commit messages and public artifacts. Existing plaintext publishers remain held.

## Reproduction and limitations
Synthetic tests include positive arithmetic/negative log counterexamples, minimum stake vs cash/risk cap, loss states, probability normalization, nonfinite input, no borrowing, duration sensitivity, and zero-risk comparison. The demonstration packaged by CI is explicitly synthetic and is not data from any wallet or market.

References:
- https://arxiv.org/abs/1603.06183 (Busseti, Ryu, Boyd): growth and drawdown trade-offs. This module does not reproduce its drawdown bound optimizer.
- https://arxiv.org/abs/2604.11577 (Long): finite mutually exclusive outcomes under stated conditions. This module does not claim or rely on its full support theorem.
