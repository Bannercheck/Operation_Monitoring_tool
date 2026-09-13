const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, options)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export const api = {
  health: () => request('/health'),
  upload: (file) => { const f = new FormData(); f.append('file', file); return request('/upload', { method: 'POST', body: f }) },
  analyze: (body) => request('/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  signals: () => request('/signals'),
  incidents: () => request('/incidents'),
  incident: (id) => request(`/incidents/${id}`),
  actions: () => request('/actions'),
}
