export function stripAccents(value: string): string {
  return value.normalize("NFKD").replace(/\p{Diacritic}/gu, "");
}
