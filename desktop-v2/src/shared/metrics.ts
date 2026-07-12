import type { Account } from "./types.js";

export interface TrialMetrics {
  total: number;
  checked: number;
  eligible: number;
  ineligible: number;
  eligibilityPercentage: number;
  coveragePercentage: number;
}

export function calculateTrialMetrics(accounts: Account[]): TrialMetrics {
  const checked = accounts.filter((account) => account.trialEligible !== null);
  const eligible = checked.filter((account) => account.trialEligible === true).length;
  const ineligible = checked.length - eligible;
  return {
    total: accounts.length,
    checked: checked.length,
    eligible,
    ineligible,
    eligibilityPercentage: checked.length ? Math.round(eligible / checked.length * 100) : 0,
    coveragePercentage: accounts.length ? Math.round(checked.length / accounts.length * 100) : 0,
  };
}
