import { HashRouter, Route, Routes } from 'react-router-dom'

function Home() {
  return (
    <main className="mx-auto max-w-3xl p-6">
      <h1 className="text-2xl font-semibold">Baskets</h1>
      <p className="mt-2 text-gray-600">
        Build custom baskets and benchmarks. Coming soon.
      </p>
    </main>
  )
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/" element={<Home />} />
      </Routes>
    </HashRouter>
  )
}
