export function scoreToGrade(score: number): string {
  if (score >= 90) return "S";
  if (score >= 80) return "A";
  if (score >= 70) return "B";
  if (score >= 55) return "C";
  if (score >= 40) return "D";
  return "F";
}

export function scoreToBand(score: number): string {
  if (score >= 80) return "Good";
  if (score >= 60) return "Fair";
  if (score >= 40) return "Needs Work";
  return "Poor";
}

export function degradationBand(fillPct: number): string {
  if (fillPct < 0.5) return "Safe";
  if (fillPct < 0.7) return "Moderate";
  if (fillPct < 0.8) return "Warning";
  return "Danger";
}
