import { useState, useEffect, useRef } from 'react';
import { Save, Trash2, Check, ChevronRight, Pencil } from 'lucide-react';
import { api } from '../../api/client';
import type { ProfileData } from '../../types';
import { toast } from 'sonner';

interface ProfilePanelProps {
  activeProfile: string | null;
  onProfileApplied: (name: string | null) => void;
}

export function ProfilePanel({ activeProfile, onProfileApplied }: ProfilePanelProps) {
  const [profiles, setProfiles] = useState<ProfileData[]>([]);
  const [loading, setLoading] = useState(true);

  // Save form
  const [isSaveOpen, setIsSaveOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const saveInputRef = useRef<HTMLInputElement>(null);

  // Inline delete confirmation
  const [deletingName, setDeletingName] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // Inline rename
  const [renamingName, setRenamingName] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [isRenaming, setIsRenaming] = useState(false);
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Apply in-flight
  const [applyingName, setApplyingName] = useState<string | null>(null);

  async function fetchProfiles() {
    try {
      const data = await api.profiles();
      setProfiles(data.profiles);
      onProfileApplied(data.active);
    } catch {
      toast.error('Failed to load profiles');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchProfiles(); }, []);

  useEffect(() => {
    if (isSaveOpen) saveInputRef.current?.focus();
    else setNewName('');
  }, [isSaveOpen]);

  useEffect(() => {
    if (renamingName) renameInputRef.current?.focus();
  }, [renamingName]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    try {
      setIsSaving(true);
      await api.saveProfile(newName.trim());
      toast.success(`Profile "${newName.trim()}" saved`);
      setIsSaveOpen(false);
      await fetchProfiles();
    } catch (e: any) {
      toast.error('Failed to save: ' + (e.message || String(e)));
    } finally {
      setIsSaving(false);
    }
  }

  async function handleApply(name: string) {
    try {
      setApplyingName(name);
      await api.applyProfile(name);
      onProfileApplied(name);
      toast.success(`"${name}" applied`);
    } catch (e: any) {
      toast.error(`Failed to apply "${name}": ` + (e.message || String(e)));
    } finally {
      setApplyingName(null);
    }
  }

  async function handleDeleteConfirm(name: string) {
    try {
      setIsDeleting(true);
      await api.deleteProfile(name);
      toast.success(`"${name}" deleted`);
      if (activeProfile === name) onProfileApplied(null);
      setDeletingName(null);
      await fetchProfiles();
    } catch (e: any) {
      toast.error('Failed to delete: ' + (e.message || String(e)));
    } finally {
      setIsDeleting(false);
    }
  }

  async function handleRename(e: React.FormEvent, oldName: string) {
    e.preventDefault();
    if (!renameValue.trim() || renameValue.trim() === oldName) {
      setRenamingName(null);
      return;
    }
    try {
      setIsRenaming(true);
      await api.renameProfile(oldName, renameValue.trim());
      toast.success(`Renamed to "${renameValue.trim()}"`);
      if (activeProfile === oldName) onProfileApplied(renameValue.trim());
      setRenamingName(null);
      await fetchProfiles();
    } catch (e: any) {
      toast.error('Failed to rename: ' + (e.message || String(e)));
    } finally {
      setIsRenaming(false);
    }
  }

  if (loading && profiles.length === 0) {
    return (
      <div className="bg-zinc-900 rounded-lg p-6 flex flex-col gap-4 animate-pulse">
        <div className="h-6 w-32 bg-zinc-800 rounded" />
        <div className="h-20 w-full bg-zinc-800 rounded" />
      </div>
    );
  }

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-zinc-800 bg-zinc-950/50 flex items-center gap-2">
        <Save size={16} className="text-pink-500" />
        <h2 className="text-zinc-100 font-medium">Profiles</h2>
      </div>

      {/* Save section */}
      <div className="px-4 py-3 border-b border-zinc-800">
        {isSaveOpen ? (
          <form onSubmit={handleSave} className="flex gap-2">
            <input
              ref={saveInputRef}
              type="text"
              placeholder="Profile name..."
              value={newName}
              onChange={e => setNewName(e.target.value)}
              disabled={isSaving}
              onKeyDown={e => e.key === 'Escape' && setIsSaveOpen(false)}
              className="flex-1 min-w-0 bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-sm focus:outline-none focus:border-pink-500 focus:ring-1 focus:ring-pink-500 disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!newName.trim() || isSaving}
              className="px-3 py-1.5 bg-pink-600 hover:bg-pink-500 disabled:bg-zinc-800 disabled:text-zinc-500 text-white rounded text-sm font-medium transition shrink-0"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => setIsSaveOpen(false)}
              className="px-3 py-1.5 text-zinc-400 hover:text-zinc-200 rounded text-sm transition shrink-0"
            >
              Cancel
            </button>
          </form>
        ) : (
          <button
            onClick={() => setIsSaveOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-3 py-1.5 rounded text-sm font-medium text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition border border-zinc-800 hover:border-zinc-700"
          >
            <Save size={14} />
            Save Current State
          </button>
        )}
      </div>

      {/* Profile list */}
      <div className="overflow-y-auto max-h-72 py-2 scrollbar-thin scrollbar-thumb-zinc-700 scrollbar-track-transparent">
        {profiles.length === 0 ? (
          <p className="text-center text-zinc-500 text-sm py-8 px-4">
            No profiles saved yet.
          </p>
        ) : (
          <div className="flex flex-col gap-0.5 px-2">
            {profiles.map(p => {
              const pts = Object.keys(p.curve_deltas).length;
              const badges = [
                pts > 0 ? `${pts} pts` : null,
                p.power_limit_w != null ? `${p.power_limit_w}W` : null,
                p.mem_offset_mhz != null ? `${p.mem_offset_mhz > 0 ? '+' : ''}${p.mem_offset_mhz} MHz mem` : null,
              ].filter(Boolean).join(' · ');

              const isActive = activeProfile === p.name;
              const isApplying = applyingName === p.name;
              const isConfirmingDelete = deletingName === p.name;
              const isRenaming_ = renamingName === p.name;

              return (
                <div
                  key={p.name}
                  className={`group rounded-md transition ${isActive ? 'bg-zinc-800/60' : 'hover:bg-zinc-800/50'}`}
                >
                  {isConfirmingDelete ? (
                    <div className="flex items-center justify-between px-3 py-2 gap-2">
                      <span className="text-sm text-zinc-300 truncate min-w-0">Delete "{p.name}"?</span>
                      <div className="flex gap-1.5 shrink-0">
                        <button
                          onClick={() => handleDeleteConfirm(p.name)}
                          disabled={isDeleting}
                          className="px-2 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white font-medium transition disabled:opacity-50"
                        >
                          Delete
                        </button>
                        <button
                          onClick={() => setDeletingName(null)}
                          className="px-2 py-1 text-xs rounded bg-zinc-700 hover:bg-zinc-600 text-zinc-300 transition"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : isRenaming_ ? (
                    <form onSubmit={e => handleRename(e, p.name)} className="flex items-center gap-2 px-3 py-2">
                      <input
                        ref={renameInputRef}
                        type="text"
                        value={renameValue}
                        onChange={e => setRenameValue(e.target.value)}
                        disabled={isRenaming}
                        onKeyDown={e => e.key === 'Escape' && setRenamingName(null)}
                        className="flex-1 min-w-0 bg-zinc-950 border border-zinc-600 rounded px-2 py-1 text-sm focus:outline-none focus:border-pink-500 focus:ring-1 focus:ring-pink-500 disabled:opacity-50"
                      />
                      <button
                        type="submit"
                        disabled={!renameValue.trim() || isRenaming}
                        className="px-2 py-1 text-xs rounded bg-zinc-700 hover:bg-zinc-600 text-zinc-200 font-medium transition disabled:opacity-50 shrink-0"
                      >
                        OK
                      </button>
                      <button
                        type="button"
                        onClick={() => setRenamingName(null)}
                        className="px-2 py-1 text-xs rounded text-zinc-500 hover:text-zinc-300 transition shrink-0"
                      >
                        Cancel
                      </button>
                    </form>
                  ) : (
                    <div className="flex items-center justify-between px-3 py-2">
                      <div className="flex items-center gap-2 min-w-0 pr-2">
                        {isActive
                          ? <Check size={13} className="text-emerald-400 shrink-0" />
                          : <span className="w-[13px] shrink-0" />
                        }
                        <div className="min-w-0">
                          <p className={`text-sm font-medium truncate ${isActive ? 'text-zinc-100' : 'text-zinc-300'}`}>
                            {p.name}
                          </p>
                          {badges && <p className="text-xs text-zinc-500">{badges}</p>}
                        </div>
                      </div>

                      <div className="flex gap-1 shrink-0 opacity-100 lg:opacity-0 group-hover:opacity-100 transition-opacity">
                        <button
                          onClick={() => handleApply(p.name)}
                          disabled={isApplying}
                          className="flex items-center gap-1 px-2 py-1 text-xs rounded text-zinc-400 hover:text-emerald-400 hover:bg-zinc-700 transition disabled:opacity-50 font-medium"
                        >
                          {isApplying
                            ? <span className="w-3 h-3 border border-zinc-500 border-t-emerald-400 rounded-full animate-spin" />
                            : <ChevronRight size={13} />
                          }
                          Apply
                        </button>
                        <button
                          onClick={() => { setRenamingName(p.name); setRenameValue(p.name); }}
                          className="p-1.5 text-zinc-600 hover:text-zinc-300 hover:bg-zinc-700 rounded transition"
                          title="Rename"
                        >
                          <Pencil size={13} />
                        </button>
                        <button
                          onClick={() => setDeletingName(p.name)}
                          className="p-1.5 text-zinc-600 hover:text-red-400 hover:bg-zinc-700 rounded transition"
                          title="Delete"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
