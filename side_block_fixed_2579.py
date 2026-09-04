                # ---- score resolve (FIX 2026-08-10, structure repaired 2026-08-11) ----
                _side, _why = "NO_TRADE", "scores unresolved"
                try:
                    _scored_snap = getattr(self, "_prefilter_candidates", None) or self.candidates or []
                    _score_by_sid = {str(_x.get("security_id","")): _x for _x in _scored_snap}
                    if _lc is not None and _lscore is None:
                        _hit = _score_by_sid.get(str(_lc.get("security_id","")))
                        if _hit is not None and _hit.get("long_score") is not None:
                            _lscore = _hit.get("long_score"); _lc["long_score"] = _lscore
                        else:
                            _Li, _Si = _inline_score(_lc, "LONG")
                            if _Li is not None:
                                _lscore = _Li; _lc["long_score"] = _Li
                                if _sscore is None: _sscore = _Si
                    if _shc is not None and _sscore is None:
                        _hit = _score_by_sid.get(str(_shc.get("security_id","")))
                        if _hit is not None and _hit.get("short_score") is not None:
                            _sscore = _hit.get("short_score"); _shc["short_score"] = _sscore
                        else:
                            _Li, _Si = _inline_score(_shc, "SHORT")
                            if _Si is not None:
                                _sscore = _Si; _shc["short_score"] = _Si
                    log.info(f"SCORE RESOLVE: L={_lscore} S={_sscore} "
                             f"(snap={len(_scored_snap)} lc_sid={_lc.get('security_id') if _lc else None} "
                             f"shc_sid={_shc.get('security_id') if _shc else None})")
                except Exception as _bf:
                    log.warning(f"score resolve skip: {_bf}")
                # ---- side selection: OWN try, _side always assigned ----
                try:
                    _side, _why = _pick_side(_regime, _lscore, _sscore, sector_leading=_sec_leading)
                except Exception as _pe:
                    _side, _why = ("LONG" if long_result else "NO_TRADE"), f"pick_side error: {_pe}"
                log.info(f"SIDE SELECT: {_side} | L={_lscore} S={_sscore} regime={_regime} | {_why}")
