export function classify(text) {
  if (/counter|matchup|strong against|weak against|good against|bad against/i.test(text)) return 'matchups';
  if (/rune|keystone/i.test(text)) return 'runes';
  if (/build|item|boots|starting|core/i.test(text)) return 'builds';
  return null;
}
export function normalize(snapshot) {
  const patch = snapshot.text.match(/\bpatch\s*[:?]?\s*(\d{1,2}\.\d{1,2})\b/i)?.[1] ?? null;
  const sections = snapshot.sections.map(s => ({...s, category:classify(s.heading)})).filter(s => s.category);
  return {patch, sections, coverage:Object.fromEntries(['builds','runes','matchups'].map(k=>[k,sections.some(s=>s.category===k)]))};
}
export function csvCell(value) { return '"' + String(value ?? '').replace(/^[=+@-]/, "'$&").replaceAll('"','""') + '"'; }