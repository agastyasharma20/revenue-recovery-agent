import type { Incident } from '../types'

export function IncidentBanner({ incident }: { incident: Incident }) {
  return (
    <div className="banner banner-danger">
      <span>🚨</span>
      <span>
        <strong>SYSTEMIC INCIDENT:</strong> {incident.message}
      </span>
    </div>
  )
}
