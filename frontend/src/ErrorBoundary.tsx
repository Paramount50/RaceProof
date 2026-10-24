import { Component, type ErrorInfo, type ReactNode } from 'react'

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('RaceProof dashboard render failed', error, info)
  }
  render() {
    if (this.state.failed) return <main><div className="error-banner"><span>The dashboard could not render this update. The backend run is still active. Reload to reconnect.</span></div></main>
    return this.props.children
  }
}
