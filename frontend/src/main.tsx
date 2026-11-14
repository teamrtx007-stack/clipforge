import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Link, NavLink, Outlet, RouterProvider } from "react-router-dom";
import { api } from "./api";
import { Icon } from "./components/Icon";
import { bytes } from "./format";
import { usePoll } from "./hooks";
import ClipEditor from "./pages/ClipEditor";
import Home from "./pages/Home";
import LiveStudio from "./pages/LiveStudio";
import ProjectPage from "./pages/Project";
import Queue from "./pages/Queue";
import Videos from "./pages/Videos";
import "./styles.css";
import { UploadsProvider } from "./uploads";

function Sidebar() {
  const { data: jobs } = usePoll(api.jobs, 4000);
  const { data: projects } = usePoll(api.projects, 8000);
  const { data: channels } = usePoll(api.channels, 5000);
  const { data: system } = usePoll(api.system, 60000);
  const active = jobs?.filter((j) => j.status === "queued" || j.status === "running").length ?? 0;
  const videos = projects?.filter((p) => !p.channel_id).length ?? 0;
  return (
    <aside className="sidebar">
      <Link to="/" className="wordmark" aria-label="ClipForge home">
        <span className="logo" aria-hidden="true"><Icon name="scissors" size={17} /></span>
        <span>ClipForge</span>
      </Link>
      <nav className="side-nav" aria-label="Main">
        <NavLink to="/" end><Icon name="home" />Home</NavLink>
        <NavLink to="/videos"><Icon name="film" />All videos{videos > 0 && <span className="count">{videos}</span>}</NavLink>
        <NavLink to="/queue"><Icon name="list" />Queue{active > 0 && <span className="count hot">{active}</span>}</NavLink>
      </nav>
      <div className="side-label">Creators</div>
      <nav className="side-nav creators" aria-label="Creators">
        {channels?.map((c) => (
          <NavLink key={c.id} to={`/projects/${c.project_id}`}>
            <span className={`avatar ${c.runtime.status}`}>{c.name[0]?.toUpperCase()}</span>
            <span className="label">{c.name}</span>
          </NavLink>
        ))}
        <NavLink to="/live" end><Icon name="plusCircle" />New creator</NavLink>
      </nav>
      {system && (
        <div className="side-card" title="Transcription and editing run on this computer. No paid APIs.">
          <span>Free · runs on this computer</span>
          <b>Whisper {system.whisper_model}</b>
          <span>{bytes(system.disk_free_bytes)} free on disk</span>
        </div>
      )}
    </aside>
  );
}

function Shell() {
  return (
    <div className="app">
      <Sidebar />
      <main>
        <div className="content">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { path: "/", element: <Home /> },
      { path: "/videos", element: <Videos /> },
      { path: "/projects/:id", element: <ProjectPage /> },
      { path: "/clips/:id", element: <ClipEditor /> },
      { path: "/live", element: <LiveStudio /> },
      { path: "/queue", element: <Queue /> },
      { path: "*", element: <p>Page not found. <Link to="/">Go home</Link></p> },
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
