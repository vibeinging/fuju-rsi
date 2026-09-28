import { useEffect, useState } from 'react'
import { OptimizationWorkspace } from './components/OptimizationWorkspace'
import './components/optimization.css'

export default function App() {
  const [theme, setTheme] = useState(() => {
    try { return window.localStorage.getItem('fuju-rsi.appearance') === 'dark' ? 'dark' : 'light' } catch { return 'light' }
  })
  useEffect(() => { try { window.localStorage.setItem('fuju-rsi.appearance', theme) } catch { /* 本地存储可选。 */ } }, [theme])
  return <div className="app yt-shell yt-shell-optimize" data-theme={theme}>
    <header className="yt-shell-header">
      <span className="yt-brand"><span aria-hidden="true">复</span><b>局 Fuju</b><small>RSI 优化实验</small></span>
      <button className="opt-button yt-theme-button" aria-label={theme === 'light' ? '切换深色外观' : '切换浅色外观'} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? '☾ 深色' : '☀ 浅色'}</button>
    </header>
    <OptimizationWorkspace />
  </div>
}
