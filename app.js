import {playlists} from "./game-data.js";

let state={code:"",host:"",currentPlayer:"",players:[],playlist:null,track:null,answers:[],started:0,buzzedBy:"",eliminated:[],finished:false};
let pendingVideo=null;
const $=id=>document.getElementById(id);
const show=id=>["home","lobby","game"].forEach(x=>$(x).classList.toggle("hidden",x!==id));
const safe=s=>s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));

function render(){
  const playersHtml=state.players.map(p=>`<div class="player">${safe(p.name)}<span class="score">${p.score} pts</span></div>`).join("");
  $("players").innerHTML=playersHtml;
  $("scoreboard").innerHTML=playersHtml;
  $("count").textContent=`(${state.players.length})`;
}

function loadVideo(track,autoplay=false){
  pendingVideo=track;
  if(!track)return;
  const id=encodeURIComponent(track.videoId);
  $("player").innerHTML=`<iframe title="YouTube soundbite" src="https://www.youtube.com/embed/${id}?start=0&end=10&autoplay=${autoplay?1:0}&controls=0&disablekb=1&modestbranding=1&rel=0&playsinline=1" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>`;
}

function setLibrary(){
  $("playlist").innerHTML=playlists.map((playlist,index)=>`<option value="${index}">${playlist.name}</option>`).join("");
}

$("leave").onclick=()=>show("home");

$("host-form").onsubmit=event=>{
  event.preventDefault();
  const name=$("host-name").value;
  state={code:String(Math.floor(1000+Math.random()*9000)),host:name,currentPlayer:name,players:[{name,score:0}],playlist:null,track:null,answers:[],started:0,buzzedBy:"",eliminated:[],finished:false};
  setLibrary();$("code").textContent=state.code;$("game-code").textContent=state.code;render();show("lobby");
};

$("join-form").onsubmit=event=>{
  event.preventDefault();
  if(!$("join-code").value.match(/^\d{4}$/)){ $("join-error").textContent="Enter a four-digit code.";return; }
  state.code=$("join-code").value;state.currentPlayer=$("join-name").value;state.players.push({name:state.currentPlayer,score:0});
  $("code").textContent=state.code;$("game-code").textContent=state.code;setLibrary();render();show("lobby");
};

$("start").onclick=()=>{
  state.playlist=playlists[+$("playlist").value];
  state.track=state.playlist.tracks[Math.floor(Math.random()*state.playlist.tracks.length)];
  state.answers=[];state.started=Date.now();state.buzzedBy="";state.eliminated=[];state.finished=false;
  $("title").textContent="Name that sound";$("status").textContent=`Playing a surprise from ${state.playlist.name}. Buzz when you know it.`;
  $("feed").textContent="";show("game");render();loadVideo(state.track);
};

$("play-clip").onclick=()=>{
  if(state.track)loadVideo(state.track,true);
};

$("buzz").onclick=()=>{
  if(!state.track||state.finished||state.buzzedBy||state.eliminated.includes(state.currentPlayer))return;
  state.buzzedBy=state.currentPlayer;$("status").textContent=`${state.currentPlayer} buzzed in — answer now!`;render();
};

$("answer-form").onsubmit=event=>{
  event.preventDefault();
  if(!state.track||state.buzzedBy!==state.currentPlayer||state.finished)return;
  const value=$("answer").value.trim().toLowerCase();
  const player=state.players.find(item=>item.name===state.currentPlayer);
  if(value!==state.track.title.toLowerCase()){
    player.score-=50;state.eliminated.push(state.currentPlayer);state.buzzedBy="";
    state.answers.push({name:state.currentPlayer,points:-50,wrong:true});
    $("status").textContent="Wrong answer: −50 points. You cannot buzz again this round.";
  }else{
    const points=100;player.score+=points;state.answers.push({name:player.name,points});state.finished=true;
    $("status").textContent=`Correct! +${points} points`;
  }
  $("feed").innerHTML=state.answers.map(answer=>`⚡ <b>${safe(answer.name)}</b> ${answer.wrong?"missed":"got it"} (${answer.points>0?"+":""}${answer.points})`).join("<br>");
  $("answer").value="";render();
};

setInterval(()=>{
  if(!state.started)return;
  const left=Math.max(0,12-Math.floor((Date.now()-state.started)/1000));
  $("timer").textContent=left?`${left}s left`:"Round over";
  if(!left)state.finished=true;
  const canBuzz=!state.finished&&!state.buzzedBy&&!state.eliminated.includes(state.currentPlayer);
  $("buzz").disabled=!canBuzz;
  $("answer-form").classList.toggle("hidden",state.buzzedBy!==state.currentPlayer||state.finished);
},250);
