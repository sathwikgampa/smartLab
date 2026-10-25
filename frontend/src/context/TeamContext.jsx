import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, setActiveTeamId as setApiActiveTeamId } from "../lib/api";
import { useApp } from "./AppContext";

const TeamContext = createContext(null);

export function TeamProvider({ children }) {
  const { account } = useApp();
  const [teams, setTeams] = useState([]);
  const [loading, setLoading] = useState(false);
  const [activeTeamId, setActiveTeamIdState] = useState(() => {
    try {
      const saved = localStorage.getItem("smartlab-active-team-id");
      return saved ? Number(saved) : null;
    } catch {
      return null;
    }
  });

  const refreshTeams = useCallback(async () => {
    if (!account) {
      setTeams([]);
      return [];
    }
    setLoading(true);
    try {
      const list = await api.teams();
      setTeams(list);
      return list;
    } catch {
      return [];
    } finally {
      setLoading(false);
    }
  }, [account]);

  useEffect(() => {
    if (account) {
      refreshTeams().then((list) => {
        if (!list || list.length === 0) return;
        setActiveTeamIdState((prev) => {
          const valid = list.some((t) => t.id === prev);
          const next = valid ? prev : list[0].id;
          try {
            localStorage.setItem("smartlab-active-team-id", String(next));
          } catch {
            /* ignore */
          }
          return next;
        });
      });
    } else {
      setTeams([]);
      setActiveTeamIdState(null);
      setApiActiveTeamId(null);
    }
  }, [account, refreshTeams]);

  const setActiveTeamId = useCallback((id) => {
    const num = id ? Number(id) : null;
    setActiveTeamIdState(num);
    setApiActiveTeamId(num);
    try {
      if (num) localStorage.setItem("smartlab-active-team-id", String(num));
      else localStorage.removeItem("smartlab-active-team-id");
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    setApiActiveTeamId(activeTeamId);
  }, [activeTeamId]);

  useEffect(() => {
    const handleInvalid = () => {
      refreshTeams().then((list) => {
        if (list && list.length > 0) {
          setActiveTeamId(list[0].id);
        }
      });
    };
    window.addEventListener("smartlab:team-invalid", handleInvalid);
    return () => window.removeEventListener("smartlab:team-invalid", handleInvalid);
  }, [refreshTeams, setActiveTeamId]);

  const activeTeam = useMemo(() => {
    if (!teams.length) return null;
    return teams.find((t) => t.id === activeTeamId) || teams[0];
  }, [teams, activeTeamId]);

  const value = useMemo(() => ({
    teams,
    activeTeam,
    activeTeamId: activeTeam?.id ?? null,
    setActiveTeamId,
    refreshTeams,
    loading,
    role: activeTeam?.role || "viewer",
    isOwner: activeTeam?.role === "owner",
    isAdminOrOwner: activeTeam?.role === "admin" || activeTeam?.role === "owner",
  }), [teams, activeTeam, setActiveTeamId, refreshTeams, loading]);

  return <TeamContext.Provider value={value}>{children}</TeamContext.Provider>;
}

export function useTeam() {
  const ctx = useContext(TeamContext);
  if (!ctx) throw new Error("useTeam must be used within a TeamProvider");
  return ctx;
}
