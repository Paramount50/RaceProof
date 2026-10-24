import type { RunReport } from './types'

export type RepairAttempt = RunReport['attempts'][number]

export function describeRepairAttempt(attempt: RepairAttempt | undefined, finalStatus: string) {
  if (!attempt) {
    return {
      diff: '',
      files: [] as string[],
      application: 'Not attempted',
      verification: 'Not attempted',
      candidateStatus: finalStatus,
    }
  }
  const application = attempt.patch_validation
    ? attempt.patch_validation.accepted ? 'Applied to isolated candidate' : 'Rejected before execution'
    : 'Not applied'
  const verification = attempt.verification
    ? attempt.verification.passed ? 'All deterministic gates passed' : 'Deterministic gates failed'
    : 'Not executed'
  return {
    diff: attempt.generated_diff ?? attempt.proposal?.unified_diff ?? '',
    files: attempt.patch_validation?.changed_paths
      ?? attempt.proposal?.file_replacements.map(item => item.path)
      ?? [],
    application,
    verification,
    candidateStatus: finalStatus,
  }
}
