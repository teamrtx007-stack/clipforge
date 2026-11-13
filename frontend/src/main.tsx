import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Link, NavLink, Outlet, RouterProvider } from "react-router-dom";
import { api } from "./api";
import { usePoll } from "./hooks";
import ClipEditor from "./pages/ClipEditor";
import Dashboard from "./pages/Dashboard";
import LiveStudio from "./pages/LiveStudio";
import ProjectPage from "./pages/Project";
import Queue from "./pages/Queue";
import "./styles.css";
import { UploadsProvider } from "./uploads";

function Shell() {
  const { data: jobs } = usePoll(api.jobs, 4000);
  const { data: system } = usePoll(api.system, 60000);
  const active = jobs?.filter((j) => j.status === "queued" || j.status === "running").length ?? 0;
  return (
    <>
      <header className="slate">
        <Link to="/" className="wordmark" aria-label="ClipForge home">
          <svg width="16" height="26" viewBox="0 0 16 26" aria-hidden="true">
            <rect x="0.75" y="0.75" width="14.5" height="24.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <circle cx="8" cy="13" r="4.2" fill="none" stroke="#e2372b" strokeWidth="2" />
          </svg>
          <span>ClipForge</span>
        </Link>
        <nav>
          <NavLink to="/" end>Projects</NavLink>
          <NavLink to="/live">Live studio</NavLink>
          <NavLink to="/queue">
            Queue{active > 0 && <span className="count">{active}</span>}
          </NavLink>
        </nav>
        {system && (
          <span className="engine" title="Transcription and editing run on this computer. No paid APIs.">
            Runs locally · Whisper {system.whisper_model}
            {system.llm.enabled ? ` · ${system.llm.model}` : ""}
          </span>
        )}
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}

const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { path: "/", element: <Dashboard /> },
      { path: "/projects/:id", element: <ProjectPage /> },
      { path: "/clips/:id", element: <ClipEditor /> },
      { path: "/live", element: <LiveStudio /> },
      { path: "/queue", element: <Queue /> },
      { path: "*", element: <p>Page not found. <Link to="/">Go to projects</Link></p> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <UploadsProvider>
      <RouterProvider router={router} />
    </UploadsProvider>
  </StrictMode>,
);
