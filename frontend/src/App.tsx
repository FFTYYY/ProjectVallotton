import { useEffect } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

import { AppLayout } from "./components/AppLayout";
import { ChatView } from "./components/ChatView";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { EmptyState } from "./components/EmptyState";
import { SettingsPage } from "./components/SettingsPage";
import { LibraryView } from "./components/LibraryView";
import { Login } from "./components/Login";
import { ToastStack } from "./components/ToastStack";
import { useAuth } from "./store/auth";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { checked, authenticated } = useAuth();
  const location = useLocation();
  if (!checked) {
    return (
      <div className="login-page">
        <span className="spinner" />
      </div>
    );
  }
  if (!authenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

export default function App() {
  const check = useAuth((s) => s.check);
  useEffect(() => {
    void check();
  }, [check]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<EmptyState />} />
          <Route path="c/:convId" element={<ChatView />} />
          <Route path="f/:folderId" element={<LibraryView />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <ConfirmDialog />
      <ToastStack />
    </BrowserRouter>
  );
}
