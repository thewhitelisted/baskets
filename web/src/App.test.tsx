import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import App from './App'

afterEach(cleanup)

describe('App', () => {
  it('renders the home route at #/', () => {
    window.history.replaceState(null, '', '/#/')
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Baskets' })).toBeDefined()
  })

  it('routes on the hash, not the pathname', () => {
    window.history.replaceState(null, '', '/some/path#/')
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Baskets' })).toBeDefined()
  })
})
