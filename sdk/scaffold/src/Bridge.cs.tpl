// The bridge: commands from outside into the running game, and answers back, through files (`bl live <command>`).
//
//   Modules/__MOD__/bridge/in/<id>.txt    a request: the command on the first line, then one "key=value" per line
//   Modules/__MOD__/bridge/out/<id>.json  the answer: {"id": ..., "ok": true, "result": {...}} or {"ok": false, "error": ...}
// Off unless the environment variable BANNERLORD_BRIDGE is 1 or the file Modules/__MOD__/bridge/on exists. Requests are
// read twice a second on the game's main thread, so a command may touch the scene directly.
//
// Commands:
//   ping                                   is the game there
//   state                                  the screen, the mission's scene, the player's position, troops alive
//   ground x= y=                           terrain height at a spot of the current scene
//   raycast x= y= [top=] [bottom=]         a vertical ray down at x, y (default from 50 m above the ground to 10 m below):
//                                          what it hits first, terrain or an entity's collision body, and how far down;
//                                          exclude=<hex body flags> (default the engine's focus ray filter), fixed=1 for the
//                                          ray cast variant on the fixed-step physics world. Measured in game: rays see
//                                          capsule and sphere bodies and prefabs spawned at run time (fixed=1), not the
//                                          triangle-mesh bodies of entities loaded with the scene, shipped ones included
//   teleport x= y= [z=]                    move the player (and the mount) there; z defaults to the ground
//   look bearing= [elevation=]             turn the camera, in degrees (bearing 0 looks along +y)
//   screenshot path=                       write a screenshot (a .bmp) at the end of this frame
//   spawn prefab= x= y= [z=] [yaw=]        place a prefab in the running scene; answers with its number
//   remove id=<number>|all                 take spawned prefabs out again
//   animate id= clip= [speed=]             play an animation clip on a spawned prefab that has a skeleton; answers with the
//                                          bone count and where the last bone is (moves while the clip plays)
//   bone id= [index=]                      a bone of a spawned prefab's skeleton now (position, up axis, relative to the
//                                          prefab), and the models on the skeleton and the entity with its bounding box
//   settlement id=                         a settlement of the running campaign: name, position, culture, owner
//   mapheight x= y=                        the campaign map's height at a position (map units)
//   mapmove x= y=                          put the player's party at a campaign map position (the map camera follows it)
//   sprite name=                           a UI sprite by name: size, category; loads the category and reports its sheets
//   say text=                              show a message on screen
//   quit                                   quit the game
// Compiled with the project; not yet run in the game from this template.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using TaleWorlds.Core;
using TaleWorlds.Engine;
using TaleWorlds.Library;
using TaleWorlds.MountAndBlade;
using TaleWorlds.MountAndBlade.View.Screens;
using TaleWorlds.ScreenSystem;
using Path = System.IO.Path;

namespace __MOD__
{
    static class Bridge
    {
        static bool _checked, _on;
        static float _t;
        static readonly List<GameEntity> Spawned = new List<GameEntity>();

        static string Dir => Path.Combine(SubModule.ModulePath, "bridge");

        public static void Tick(float dt)
        {
            if (!_checked)
            {
                _checked = true;
                try
                {
                    _on = Environment.GetEnvironmentVariable("BANNERLORD_BRIDGE") == "1" || File.Exists(Path.Combine(Dir, "on"));
                    if (_on) { Directory.CreateDirectory(Path.Combine(Dir, "in")); Directory.CreateDirectory(Path.Combine(Dir, "out")); Log.Write("bridge on: " + Dir); }
                }
                catch (Exception e) { _on = false; Log.Write("bridge could not start: " + e.Message); }
            }
            if (!_on) return;
            _t += dt;
            if (_t < 0.5f) return;
            _t = 0f;
            try
            {
                foreach (string f in Directory.GetFiles(Path.Combine(Dir, "in"), "*.txt"))
                {
                    string id = Path.GetFileNameWithoutExtension(f);
                    string[] lines;
                    try { lines = File.ReadAllLines(f); File.Delete(f); }
                    catch (IOException) { continue; }                       // still being written: next tick
                    string answer;
                    try { answer = "{\"id\": " + Str(id) + ", \"ok\": true, \"result\": " + Obj(Run(lines)) + "}"; }
                    catch (Exception e) { answer = "{\"id\": " + Str(id) + ", \"ok\": false, \"error\": " + Str(e.Message) + "}"; }
                    string tmp = Path.Combine(Dir, "out", id + ".tmp");
                    File.WriteAllText(tmp, answer, new UTF8Encoding(false));
                    File.Move(tmp, Path.Combine(Dir, "out", id + ".json"));    // the reader never sees half a file
                }
            }
            catch (Exception e) { Log.Write("bridge tick failed: " + e.Message); }
        }

        static Dictionary<string, object> Run(string[] lines)
        {
            if (lines.Length == 0) throw new Exception("empty request");
            string cmd = lines[0].Trim();
            var a = new Dictionary<string, string>();
            for (int i = 1; i < lines.Length; i++)
            {
                int eq = lines[i].IndexOf('=');
                if (eq > 0) a[lines[i].Substring(0, eq).Trim()] = lines[i].Substring(eq + 1);
            }
            var r = new Dictionary<string, object>();
            Mission m = Mission.Current;
            switch (cmd)
            {
                case "ping":
                    r["time"] = DateTime.Now.ToString("HH:mm:ss.fff");
                    return r;
                case "state":
                    ScreenBase top = ScreenManager.TopScreen;
                    r["screen"] = top == null ? "none" : top.GetType().Name;
                    r["in_mission"] = m != null;
                    if (m != null)
                    {
                        r["scene"] = m.SceneName;
                        int us = 0, them = 0;
                        foreach (Agent ag in m.Agents)
                            if (ag.IsHuman && ag.IsActive() && ag.Team != null) { if (ag.Team == m.PlayerTeam || ag.Team.IsPlayerAlly) us++; else them++; }
                        r["alive_us"] = us; r["alive_them"] = them;
                        Agent main = m.MainAgent;
                        if (main != null) { Vec3 p = main.Position; r["player"] = new[] { p.x, p.y, p.z }; }
                        r["spawned"] = Spawned.Count;
                    }
                    return r;
                case "ground":
                    Need(m);
                    r["z"] = m.Scene.GetGroundHeightAtPosition(new Vec3(Num(a, "x"), Num(a, "y"), 10000f));
                    return r;
                case "raycast":
                {
                    Need(m);
                    float x = Num(a, "x"), y = Num(a, "y"), g = m.Scene.GetGroundHeightAtPosition(new Vec3(x, y, 10000f));
                    float zTop = a.ContainsKey("top") ? Num(a, "top") : g + 50f, zBottom = a.ContainsKey("bottom") ? Num(a, "bottom") : g - 10f;
                    BodyFlags ex = a.ContainsKey("exclude") ? (BodyFlags)Convert.ToUInt32(a["exclude"], 16) : BodyFlags.CommonFocusRayCastExcludeFlags;
                    float dist; Vec3 at; WeakGameEntity ent;
                    bool hit = a.ContainsKey("fixed") && a["fixed"] == "1"
                        ? m.Scene.RayCastForClosestEntityOrTerrainFixedPhysics(new Vec3(x, y, zTop), new Vec3(x, y, zBottom), out dist, out at, out ent, 0.01f, ex)
                        : m.Scene.RayCastForClosestEntityOrTerrain(new Vec3(x, y, zTop), new Vec3(x, y, zBottom), out dist, out at, out ent, 0.01f, ex);
                    r["hit"] = hit; r["terrain_z"] = g;
                    if (hit) { r["z"] = at.z; r["distance"] = dist; r["entity"] = ent.IsValid ? ent.Name : null; }
                    return r;
                }
                case "teleport":
                {
                    Need(m);
                    Agent main = m.MainAgent;
                    if (main == null || !main.IsActive()) throw new Exception("no player agent in this mission");
                    var pos = new Vec3(Num(a, "x"), Num(a, "y"), 0f);
                    pos.z = a.ContainsKey("z") ? Num(a, "z") : m.Scene.GetGroundHeightAtPosition(new Vec3(pos.x, pos.y, 10000f));
                    (main.MountAgent ?? main).TeleportToPosition(pos);
                    r["position"] = new[] { pos.x, pos.y, pos.z };
                    return r;
                }
                case "look":
                {
                    var ms = ScreenManager.TopScreen as MissionScreen;
                    if (ms == null) throw new Exception("no mission screen");
                    ms.CameraBearing = Num(a, "bearing") * (float)Math.PI / 180f;
                    if (a.ContainsKey("elevation"))
                        typeof(MissionScreen).GetProperty("CameraElevation").SetValue(ms, Num(a, "elevation") * (float)Math.PI / 180f);
                    return r;
                }
                case "screenshot":
                    if (!a.ContainsKey("path")) throw new Exception("screenshot needs path=");
                    Directory.CreateDirectory(Path.GetDirectoryName(a["path"]));
                    Utilities.TakeScreenshot(a["path"]);
                    r["path"] = a["path"];
                    return r;
                case "spawn":
                {
                    Need(m);
                    if (!a.ContainsKey("prefab")) throw new Exception("spawn needs prefab=");
                    var pos = new Vec3(Num(a, "x"), Num(a, "y"), 0f);
                    pos.z = a.ContainsKey("z") ? Num(a, "z") : m.Scene.GetGroundHeightAtPosition(new Vec3(pos.x, pos.y, 10000f));
                    MatrixFrame frame = MatrixFrame.Identity;
                    frame.origin = pos;
                    if (a.ContainsKey("yaw")) frame.rotation.RotateAboutUp(Num(a, "yaw") * (float)Math.PI / 180f);
                    GameEntity e = GameEntity.Instantiate(m.Scene, a["prefab"], frame);
                    if (e == null) throw new Exception("no prefab named " + a["prefab"]);
                    Spawned.Add(e);
                    r["id"] = Spawned.Count - 1; r["position"] = new[] { pos.x, pos.y, pos.z };
                    return r;
                }
                case "remove":
                {
                    string which = a.ContainsKey("id") ? a["id"] : "all";
                    int n = 0;
                    for (int i = 0; i < Spawned.Count; i++)
                    {
                        if (Spawned[i] == null || (which != "all" && which != i.ToString(CultureInfo.InvariantCulture))) continue;
                        Spawned[i].Remove(0); Spawned[i] = null; n++;
                    }
                    r["removed"] = n;
                    return r;
                }
                case "animate":
                case "bone":
                {
                    if (!a.ContainsKey("id")) throw new Exception(cmd + " needs id=");
                    int id = (int)Num(a, "id");
                    if (id < 0 || id >= Spawned.Count || Spawned[id] == null) throw new Exception("no spawned prefab " + id);
                    Skeleton sk = Spawned[id].Skeleton;
                    if (sk == null) throw new Exception("spawned prefab " + id + " has no skeleton");
                    if (cmd == "animate")
                    {
                        if (!a.ContainsKey("clip")) throw new Exception("animate needs clip=");
                        sk.SetAnimationAtChannel(a["clip"], 0, a.ContainsKey("speed") ? Num(a, "speed") : 1f, 0f, 0f);
                    }
                    sk.ForceUpdateBoneFrames();
                    sbyte n = sk.GetBoneCount(); r["bones"] = (int)n;
                    sbyte bi = a.ContainsKey("index") ? (sbyte)Num(a, "index") : (sbyte)(n - 1);
                    MatrixFrame bf = sk.GetBoneEntitialFrame(bi); Vec3 o = bf.origin; Vec3 up = bf.rotation.u;
                    r["bone"] = sk.GetBoneName(bi); r["bone_position"] = new[] { o.x, o.y, o.z };
                    r["bone_up"] = new[] { up.x, up.y, up.z };                       // tilts when the clip bends the bone
                    r["skeleton"] = sk.GetName();
                    r["skeleton_models"] = sk.GetComponentCount(GameEntity.ComponentType.MetaMesh);
                    r["entity_models"] = Spawned[id].GetComponentCount(GameEntity.ComponentType.MetaMesh);
                    Vec3 lo = Spawned[id].GetBoundingBoxMin(), hi = Spawned[id].GetBoundingBoxMax();
                    r["box_min"] = new[] { lo.x, lo.y, lo.z }; r["box_max"] = new[] { hi.x, hi.y, hi.z };
                    return r;
                }
                case "settlement":
                {
                    if (!a.ContainsKey("id")) throw new Exception("settlement needs id=");
                    if (TaleWorlds.CampaignSystem.Campaign.Current == null) throw new Exception("no campaign is running");
                    var st = TaleWorlds.CampaignSystem.Settlements.Settlement.Find(a["id"]);
                    if (st == null) throw new Exception("no settlement " + a["id"]);
                    r["name"] = st.Name.ToString(); Vec2 sp = st.GatePosition.ToVec2(); r["gate_position"] = new[] { sp.x, sp.y };
                    r["culture"] = st.Culture?.StringId; r["owner_clan"] = st.OwnerClan?.StringId;
                    return r;
                }
                case "mapheight":
                {
                    if (TaleWorlds.CampaignSystem.Campaign.Current == null) throw new Exception("no campaign is running");
                    var at = new TaleWorlds.CampaignSystem.CampaignVec2(new Vec2(Num(a, "x"), Num(a, "y")), true); float h = 0f;
                    r["found"] = TaleWorlds.CampaignSystem.Campaign.Current.MapSceneWrapper.GetHeightAtPoint(in at, ref h); r["height"] = h;
                    return r;
                }
                case "mapmove":
                {
                    if (TaleWorlds.CampaignSystem.Campaign.Current == null) throw new Exception("no campaign is running");
                    var to = new TaleWorlds.CampaignSystem.CampaignVec2(new Vec2(Num(a, "x"), Num(a, "y")), true);
                    TaleWorlds.CampaignSystem.Party.MobileParty.MainParty.Position = to;
                    Vec2 now = TaleWorlds.CampaignSystem.Party.MobileParty.MainParty.Position.ToVec2(); r["party"] = new[] { now.x, now.y };
                    return r;
                }
                case "sprite":
                {
                    if (!a.ContainsKey("name")) throw new Exception("sprite needs name=");
                    var sd = TaleWorlds.Engine.GauntletUI.UIResourceManager.SpriteData;
                    if (sd == null) throw new Exception("the UI resources are not loaded yet");
                    if (!sd.Sprites.ContainsKey(a["name"])) throw new Exception("no sprite named " + a["name"] + " (" + sd.Sprites.Count + " sprites known)");
                    var sp = sd.Sprites[a["name"]];
                    r["width"] = sp.Width; r["height"] = sp.Height;
                    TaleWorlds.TwoDimension.SpriteCategory cat = null;
                    foreach (var kv in sd.SpriteCategories) foreach (var part in kv.Value.SpriteParts) if (part.Name == a["name"]) cat = kv.Value;
                    if (cat == null) return r;
                    r["category"] = cat.Name; r["loaded_before"] = cat.IsLoaded;
                    if (!cat.IsLoaded) cat.Load(TaleWorlds.Engine.GauntletUI.UIResourceManager.ResourceContext, TaleWorlds.Engine.GauntletUI.UIResourceManager.ResourceDepot);
                    r["loaded"] = cat.IsLoaded; r["sheets"] = cat.SpriteSheetCount;
                    int real = 0; foreach (var t in cat.SpriteSheets) if (t != null && t.Width > 0) real++;
                    r["sheets_with_a_texture"] = real;
                    return r;
                }
                case "say":
                    InformationManager.DisplayMessage(new InformationMessage(a.ContainsKey("text") ? a["text"] : ""));
                    return r;
                case "quit":
                    if (AutoTest.On) AutoTest.Done();          // the end marker for the harness, then quit
                    Utilities.QuitGame();
                    return r;
                default:
                    throw new Exception("unknown command " + cmd);
            }
        }

        static void Need(Mission m) { if (m == null || m.Scene == null) throw new Exception("no mission is running"); }

        static float Num(Dictionary<string, string> a, string key)
        {
            if (!a.ContainsKey(key)) throw new Exception("missing " + key + "=");
            if (!float.TryParse(a[key], NumberStyles.Float, CultureInfo.InvariantCulture, out float v)) throw new Exception(key + " is not a number: " + a[key]);
            return v;
        }

        // just enough JSON for the answers: strings, numbers, booleans, arrays of numbers, one level of names
        static string Str(string s)
        {
            var b = new StringBuilder("\"");
            foreach (char c in s ?? "")
            {
                if (c == '"' || c == '\\') b.Append('\\').Append(c);
                else if (c == '\n') b.Append("\\n");
                else if (c == '\r') b.Append("\\r");
                else if (c == '\t') b.Append("\\t");
                else if (c < 32) b.Append("\\u").Append(((int)c).ToString("x4"));
                else b.Append(c);
            }
            return b.Append('"').ToString();
        }

        static string Val(object v)
        {
            if (v == null) return "null";
            if (v is bool) return (bool)v ? "true" : "false";
            if (v is string) return Str((string)v);
            if (v is float) return ((float)v).ToString("R", CultureInfo.InvariantCulture);
            if (v is float[]) { var parts = new List<string>(); foreach (float f in (float[])v) parts.Add(f.ToString("R", CultureInfo.InvariantCulture)); return "[" + string.Join(", ", parts) + "]"; }
            return Convert.ToString(v, CultureInfo.InvariantCulture);
        }

        static string Obj(Dictionary<string, object> d)
        {
            var parts = new List<string>();
            foreach (var kv in d) parts.Add(Str(kv.Key) + ": " + Val(kv.Value));
            return "{" + string.Join(", ", parts) + "}";
        }
    }
}
