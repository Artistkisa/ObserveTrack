import React, { useEffect, useRef, useState } from 'react'

export default function Ghost({ active = false, size = 70 }) {
  const node = useRef(null)
  const [visible, setVisible] = useState(false)
  const [foreground, setForeground] = useState(document.visibilityState !== 'hidden')
  useEffect(() => {
    const observe = new IntersectionObserver(entries => setVisible(entries[0].isIntersecting))
    if (node.current) observe.observe(node.current)
    const update = () => setForeground(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', update)
    return () => { observe.disconnect(); document.removeEventListener('visibilitychange', update) }
  }, [])
  return <span ref={node} className={`ghost ${active && visible && foreground ? 'working' : ''}`} style={{ width: size, height: size }} aria-hidden="true">
    <svg viewBox="0 0 80 80" fill="none">
      <ellipse className="ghost-shadow" cx="40" cy="72" rx="19" ry="3" fill="currentColor" opacity=".14"/>
      <g className="ghost-body">
        <path d="M17 39C17 22 26 12 40 12s23 10 23 27v23c0 4-3 6-6 4l-8-5-9 6-9-6-8 5c-3 2-6 0-6-4V39Z" fill="currentColor"/>
        <path d="M40 12c14 0 23 10 23 27v23c0 4-3 6-6 4l-8-5-9 6V12Z" fill="#73bda8" opacity=".28"/>
        <path d="M23 32c2-8 7-13 15-14" stroke="#f0fff8" strokeWidth="3" strokeLinecap="round" opacity=".8"/>
        <g className="ghost-eyes" fill="#203b39"><ellipse cx="31" cy="37" rx="3" ry="4"/><ellipse cx="48" cy="37" rx="3" ry="4"/></g>
        <circle cx="25" cy="44" r="3" fill="#78baaa" opacity=".5"/><circle cx="54" cy="44" r="3" fill="#78baaa" opacity=".5"/>
        <path d="M36 45q4 4 8 0" stroke="#294d44" strokeWidth="2" strokeLinecap="round"/>
        <path d="M18 51q22 7 44 0v6q-22 7-44 0Z" fill="#536486"/>
        <path d="m51 56 3 13 7-4-5-10" fill="#7486b3"/>
        <path d="m26 53 6 1" stroke="#b8c6ea" strokeWidth="1.5" strokeLinecap="round"/>
        <circle cx="61" cy="22" r="10" fill="#202c3f" stroke="#b6c7e9" strokeWidth="1.5"/>
        <circle cx="60" cy="21" r="4" stroke="#b7e8d4" strokeWidth="1.6"/>
        <path d="m63 24 3 3" stroke="#b7e8d4" strokeWidth="1.8" strokeLinecap="round"/>
        <path d="m71 42 1.5 3.5L76 47l-3.5 1.5L71 52l-1.5-3.5L66 47l3.5-1.5Z" fill="#b8aedc" opacity=".8"/>
      </g>
    </svg>
  </span>
}
